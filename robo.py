"""
Radar Formaturas: robô de abastecimento do painel.
Consulta o Censo Escolar e o Censo da Educação Superior (INEP) na Base dos Dados
(Google BigQuery), projeta as turmas de formatura de 9º ano, 3º ano do médio,
técnico e faculdade nas cidades da região, e manda um CSV pelo Telegram.
"""
import csv
import json
import os
import time
from collections import defaultdict

import requests
from google.cloud import bigquery
from google.oauth2 import service_account

# ================== CONFIGURAÇÃO (pode editar) ==================
UF = "SP"
MICRORREGIOES = ["Votuporanga", "Fernandópolis", "Jales"]   # todas as cidades de cada microrregião
CIDADES_EXTRAS = []                                          # ex.: ["General Salgado"]
ANOS_FORMATURA = [2026, 2027]
LIMITE_TURMA = 30                                            # foco: turmas de até 30 alunos
# ================================================================

ESC = "basedosdados.br_inep_censo_escolar"
SUP = "basedosdados.br_inep_censo_educacao_superior"
DIR = "basedosdados.br_bd_diretorios_brasil"
LIMITE_BYTES = 200 * 10**9
ETAPA = "início"

# Códigos de etapa do Censo Escolar (dicionário do INEP)
EF = {19: 6, 20: 7, 21: 8, 41: 9}               # 6º ao 9º ano
EM = {25: 1, 26: 2, 27: 3}                       # 1ª a 3ª série do médio
INTEGRADO = {30: 1, 31: 2, 32: 3}                # técnico integrado ao médio
TECNICO = {39: "Técnico concomitante", 40: "Técnico subsequente", 64: "Técnico"}
GRAU = {"1": "Bacharelado", "2": "Licenciatura", "3": "Tecnológico", "4": "Bacharelado e Licenciatura"}
DURACAO = {"1": 4.5, "2": 4.0, "3": 2.5, "4": 4.5}  # anos, para estimar turma de curso sem concluintes


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def cliente():
    info = json.loads(os.environ["GCP_KEY"])
    cred = service_account.Credentials.from_service_account_info(info)
    return bigquery.Client(credentials=cred, project=info["project_id"])


def consulta(bq, sql, params=()):
    cfg = bigquery.QueryJobConfig(query_parameters=list(params), maximum_bytes_billed=LIMITE_BYTES)
    return list(bq.query(sql, job_config=cfg).result())


def txt(v):
    return "" if v is None or str(v) in ("None", "nan") else str(v).strip()


def num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def cidades(bq):
    rows = consulta(bq, f"""SELECT id_municipio, nome FROM `{DIR}.municipio`
        WHERE sigla_uf = @uf AND (nome_microrregiao IN UNNEST(@micros) OR nome IN UNNEST(@extras))""",
                    [bigquery.ScalarQueryParameter("uf", "STRING", UF),
                     bigquery.ArrayQueryParameter("micros", "STRING", MICRORREGIOES),
                     bigquery.ArrayQueryParameter("extras", "STRING", CIDADES_EXTRAS)])
    return {txt(r.id_municipio): txt(r.nome) for r in rows}


def projetar_escolas(turmas_por_escola, base):
    """Recebe {id_escola: [(etapa, alunos), ...]} do ano-base e projeta as formaturas."""
    resultado = {}
    for esc, turmas in turmas_por_escola.items():
        por_etapa = defaultdict(list)
        for etapa, alunos in turmas:
            por_etapa[etapa].append(alunos)
        tem_medio = any(e in por_etapa for e in list(EM) + list(INTEGRADO))
        forms = []
        for ano in ANOS_FORMATURA:
            dif = ano - base
            # 9º ano: série do fundamental no ano-base que chega ao 9º no ano da formatura
            serie_ef = 9 - dif
            etapa_ef = next((e for e, s in EF.items() if s == serie_ef), None)
            if etapa_ef and por_etapa.get(etapa_ef):
                forms.append({"formatura": "9º ano", "ano": ano, "alunos": sorted(por_etapa[etapa_ef]), "estimado": False})
            # 3º ano do médio
            serie_em = 3 - dif
            etapa_em = next((e for e, s in EM.items() if s == serie_em), None)
            if etapa_em and por_etapa.get(etapa_em):
                forms.append({"formatura": "3º ano", "ano": ano, "alunos": sorted(por_etapa[etapa_em]), "estimado": False})
            elif serie_em == 0 and tem_medio and por_etapa.get(41):
                # 9º ano de hoje vira 3º ano daqui a 3 anos, se a escola tem ensino médio
                forms.append({"formatura": "3º ano", "ano": ano, "alunos": sorted(por_etapa[41]), "estimado": True})
            etapa_int = next((e for e, s in INTEGRADO.items() if s == serie_em), None)
            if etapa_int and por_etapa.get(etapa_int):
                forms.append({"formatura": "3º ano técnico integrado", "ano": ano,
                              "alunos": sorted(por_etapa[etapa_int]), "estimado": False})
        # Técnicos (concomitante, subsequente): duram 1,5 a 2 anos; turma do ano-base forma no ano seguinte
        for etapa, nome in TECNICO.items():
            if por_etapa.get(etapa):
                forms.append({"formatura": nome, "ano": base + 2, "alunos": sorted(por_etapa[etapa]), "estimado": True})
        if forms:
            resultado[esc] = forms
    return resultado


def prioridade(turmas):
    ate30 = sum(1 for t in turmas for a in t["alunos"] if 0 < a <= LIMITE_TURMA)
    total = sum(a for t in turmas for a in t["alunos"])
    pontos = min(100, ate30 * 15 + min(total, 400) // 10)
    prio = "Alta" if ate30 >= 3 else "Média" if ate30 >= 1 else "Baixa"
    return ate30, total, pontos, prio


def buscar_escolas(bq, mun):
    global ETAPA
    ETAPA = "turmas do Censo Escolar"
    base = consulta(bq, f"SELECT MAX(ano) AS a FROM `{ESC}.turma` WHERE sigla_uf = @uf",
                    [bigquery.ScalarQueryParameter("uf", "STRING", UF)])[0].a
    etapas = [str(e) for e in list(EF) + list(EM) + list(INTEGRADO) + list(TECNICO)]
    rows = consulta(bq, f"""SELECT id_escola, id_municipio, rede, etapa_ensino, quantidade_matriculas
        FROM `{ESC}.turma` WHERE ano = @ano AND sigla_uf = @uf
        AND id_municipio IN UNNEST(@mun) AND etapa_ensino IN UNNEST(@etapas)""",
                    [bigquery.ScalarQueryParameter("ano", "INT64", int(base)),
                     bigquery.ScalarQueryParameter("uf", "STRING", UF),
                     bigquery.ArrayQueryParameter("mun", "STRING", list(mun)),
                     bigquery.ArrayQueryParameter("etapas", "STRING", etapas)])
    turmas, info = defaultdict(list), {}
    for r in rows:
        esc = txt(r.id_escola)
        turmas[esc].append((num(r.etapa_ensino), num(r.quantidade_matriculas)))
        info[esc] = (txt(r.id_municipio), txt(r.rede))
    log("Turmas lidas:", len(rows), "em", len(turmas), "escolas; ano-base", base)
    proj = projetar_escolas(turmas, int(base))

    ETAPA = "diretório de escolas"
    dir_rows = consulta(bq, f"""SELECT id_escola, nome, endereco, telefone, dependencia_administrativa,
        restricao_atendimento FROM `{DIR}.escola` WHERE id_escola IN UNNEST(@ids)""",
                        [bigquery.ArrayQueryParameter("ids", "STRING", list(proj))])
    diretorio = {txt(r.id_escola): r for r in dir_rows}

    saida = []
    for esc, forms in proj.items():
        d = diretorio.get(esc)
        if d and "PARALISADA" in txt(d.restricao_atendimento).upper():
            continue
        municipio, rede = info[esc]
        ate30, total, pontos, prio = prioridade(forms)
        etiquetas = sorted({"Técnico" if "écnico" in f["formatura"] else f["formatura"] for f in forms})
        saida.append({
            "id": f"E{esc}", "tipo": "Escola", "nome": txt(d.nome).title() if d else f"Escola {esc}",
            "municipio": mun.get(municipio, municipio), "rede": (txt(d.dependencia_administrativa) if d else "") or rede.title(),
            "endereco": txt(d.endereco).title() if d else "", "telefone": txt(d.telefone) if d else "",
            "etiquetas": "|".join(etiquetas), "turmas": json.dumps(forms, ensure_ascii=False),
            "turmas_ate30": ate30, "total_alunos": total, "prioridade": prio, "pontuacao": pontos,
            "base": f"Censo Escolar {base}"})
    return saida


def buscar_faculdades(bq, mun):
    global ETAPA
    ETAPA = "cursos do Censo da Educação Superior"
    base = consulta(bq, f"SELECT MAX(ano) AS a FROM `{SUP}.curso` WHERE sigla_uf = @uf",
                    [bigquery.ScalarQueryParameter("uf", "STRING", UF)])[0].a
    rows = consulta(bq, f"""SELECT c.id_ies, c.id_municipio, c.nome_curso, c.tipo_grau_academico, c.rede,
        c.quantidade_matriculas AS mat, c.quantidade_concluintes AS conc,
        c.quantidade_concluintes_diurno AS cd, c.quantidade_concluintes_noturno AS cn,
        c.quantidade_matriculas_diurno AS md, c.quantidade_matriculas_noturno AS mn,
        i.nome AS ies, i.sigla, i.id_municipio AS ies_mun, i.endereco, i.numero, i.bairro, i.cep
        FROM `{SUP}.curso` c LEFT JOIN `{SUP}.ies` i ON i.id_ies = c.id_ies AND i.ano = c.ano
        WHERE c.ano = @ano AND c.sigla_uf = @uf AND c.id_municipio IN UNNEST(@mun)
        AND c.tipo_modalidade_ensino = '1' AND c.tipo_nivel_academico = '1' AND c.quantidade_matriculas > 0""",
                    [bigquery.ScalarQueryParameter("ano", "INT64", int(base)),
                     bigquery.ScalarQueryParameter("uf", "STRING", UF),
                     bigquery.ArrayQueryParameter("mun", "STRING", list(mun))])
    ies = {}
    for r in rows:
        chave = (txt(r.id_ies), txt(r.id_municipio))
        if chave not in ies:
            mesmo_lugar = txt(r.ies_mun) == txt(r.id_municipio)
            end = ", ".join(x for x in [txt(r.endereco), txt(r.numero), txt(r.bairro)] if x) if mesmo_lugar else ""
            nome = txt(r.ies).title()
            if txt(r.sigla) and txt(r.sigla).upper() not in nome.upper():
                nome += f" ({txt(r.sigla)})"
            ies[chave] = {"nome": nome, "municipio": mun.get(txt(r.id_municipio), ""), "endereco": end,
                          "rede": "Pública" if txt(r.rede) == "1" else "Privada", "cursos": []}
        grau = GRAU.get(txt(r.tipo_grau_academico), "")
        turnos = [("Diurno", num(r.cd), num(r.md)), ("Noturno", num(r.cn), num(r.mn))]
        for turno, conc, mat in turnos:
            if conc > 0:
                ies[chave]["cursos"].append({"curso": txt(r.nome_curso), "grau": grau, "turno": turno,
                                             "alunos": [conc], "estimado": False})
            elif mat > 0 and num(r.conc) == 0:
                est = round(mat / DURACAO.get(txt(r.tipo_grau_academico), 4.5))
                if est > 0:
                    ies[chave]["cursos"].append({"curso": txt(r.nome_curso), "grau": grau, "turno": turno,
                                                 "alunos": [est], "estimado": True})
        if num(r.conc) > 0 and num(r.cd) == 0 and num(r.cn) == 0:
            ies[chave]["cursos"].append({"curso": txt(r.nome_curso), "grau": grau, "turno": "",
                                         "alunos": [num(r.conc)], "estimado": False})
    saida = []
    for (id_ies, id_mun), d in ies.items():
        if not d["cursos"]:
            continue
        cursos = sorted(d["cursos"], key=lambda c: (c["curso"], c["turno"]))
        ate30, total, pontos, prio = prioridade(cursos)
        saida.append({
            "id": f"F{id_ies}-{id_mun}", "tipo": "Faculdade", "nome": d["nome"], "municipio": d["municipio"],
            "rede": d["rede"], "endereco": d["endereco"].title(), "telefone": "", "etiquetas": "Faculdade",
            "turmas": json.dumps(cursos, ensure_ascii=False), "turmas_ate30": ate30, "total_alunos": total,
            "prioridade": prio, "pontuacao": pontos, "base": f"Censo da Educação Superior {base}"})
    log("Faculdades:", len(saida))
    return saida


def enviar(caminho, legenda):
    token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    with open(caminho, "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendDocument",
                          data={"chat_id": chat, "caption": legenda},
                          files={"document": (os.path.basename(caminho), f, "text/csv")}, timeout=120)
    if not r.ok:
        raise RuntimeError(f"Erro do Telegram: {r.text}")


def avisar(msg):
    try:
        token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg}, timeout=30)
    except Exception:
        pass


def main():
    global ETAPA
    bq = cliente()
    ETAPA = "cidades da região"
    mun = cidades(bq)
    log("Cidades:", len(mun), sorted(mun.values()))
    linhas = buscar_escolas(bq, mun) + buscar_faculdades(bq, mun)
    ETAPA = "montagem da planilha"
    campos = ["id", "tipo", "nome", "municipio", "rede", "endereco", "telefone", "etiquetas", "turmas",
              "turmas_ate30", "total_alunos", "prioridade", "pontuacao", "base"]
    saida = "instituicoes-formaturas.csv"
    with open(saida, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        w.writerows(linhas)
    escolas = sum(1 for l in linhas if l["tipo"] == "Escola")
    ate30 = sum(l["turmas_ate30"] for l in linhas)
    enviar(saida, f"🎓 Radar Formaturas · {len(mun)} cidades · {escolas} escolas e "
                  f"{len(linhas) - escolas} faculdades · {ate30} turmas de até {LIMITE_TURMA} alunos. "
                  f"Importe este arquivo no painel.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        avisar(f"⚠️ Radar Formaturas: parou na etapa \"{ETAPA}\": {str(e)[:3000]}")
        raise
