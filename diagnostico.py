"""
Radar Formaturas: diagnóstico 2.
Confirma o diretório de escolas (nome, endereço, telefone), os códigos das séries
e dos cursos, e manda um relatório em texto pelo Telegram.
"""
import json
import os

import requests
from google.cloud import bigquery
from google.oauth2 import service_account

LIMITE = 50 * 10**9
VOTU = "3557105"
linhas = []


def out(*a):
    texto = " ".join(str(x) for x in a)
    print(texto, flush=True)
    linhas.append(texto)


info = json.loads(os.environ["GCP_KEY"])
bq = bigquery.Client(credentials=service_account.Credentials.from_service_account_info(info),
                     project=info["project_id"])


def consulta(sql):
    return list(bq.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=LIMITE)).result())


def bloco(titulo, fn):
    try:
        out(f"\n== {titulo}")
        fn()
    except Exception as e:
        out("ERRO:", str(e)[:800])


def diretorios():
    tabs = [t.table_id for t in bq.list_tables("basedosdados.br_bd_diretorios_brasil")]
    out("tabelas:", ", ".join(tabs))
    for t in tabs:
        if "escola" in t:
            tb = bq.get_table(f"basedosdados.br_bd_diretorios_brasil.{t}")
            out(f"-- {t} ({tb.num_rows}): " + ", ".join(f"{c.name}:{c.field_type}" for c in tb.schema))
            for r in consulta(f"SELECT * FROM `basedosdados.br_bd_diretorios_brasil.{t}` "
                              f"WHERE CAST(id_municipio AS STRING) = '{VOTU}' LIMIT 4"):
                out("AMOSTRA:", json.dumps({k: str(v) for k, v in dict(r).items()}, ensure_ascii=False)[:1200])


def dic_escolar():
    for r in consulta("SELECT id_tabela, nome_coluna, chave, valor FROM `basedosdados.br_inep_censo_escolar.dicionario` "
                      "WHERE nome_coluna IN ('etapa_ensino','rede','tipo_situacao_funcionamento') ORDER BY 1,2,SAFE_CAST(chave AS INT64)"):
        out(f"{r.id_tabela}.{r.nome_coluna} {r.chave} = {r.valor}")


def dic_superior():
    for r in consulta("SELECT id_tabela, nome_coluna, chave, valor FROM `basedosdados.br_inep_censo_educacao_superior.dicionario` "
                      "ORDER BY 1,2,SAFE_CAST(chave AS INT64)"):
        out(f"{r.id_tabela}.{r.nome_coluna} {r.chave} = {r.valor}")


def turmas_votu():
    for r in consulta(f"SELECT etapa_ensino, rede, COUNT(*) turmas, SUM(quantidade_matriculas) alunos, "
                      f"COUNTIF(quantidade_matriculas <= 30) ate30 FROM `basedosdados.br_inep_censo_escolar.turma` "
                      f"WHERE ano = 2024 AND id_municipio = '{VOTU}' GROUP BY 1,2 ORDER BY SAFE_CAST(etapa_ensino AS INT64), 2"):
        out(f"etapa {r.etapa_ensino} | {r.rede} | {r.turmas} turmas | {r.alunos} alunos | {r.ate30} até 30")


def escolas_votu():
    for r in consulta(f"SELECT id_escola, rede, tipo_situacao_funcionamento, cnpj_escola_privada, cnpj_mantenedora "
                      f"FROM `basedosdados.br_inep_censo_escolar.escola` WHERE ano = 2025 AND id_municipio = '{VOTU}' LIMIT 15"):
        out(dict(r))


def cursos_votu():
    for r in consulta(f"SELECT c.id_ies, i.nome AS ies, c.nome_curso, c.tipo_modalidade_ensino, c.tipo_grau_academico, "
                      f"c.tipo_nivel_academico, c.quantidade_matriculas, c.quantidade_concluintes, "
                      f"c.quantidade_concluintes_diurno, c.quantidade_concluintes_noturno "
                      f"FROM `basedosdados.br_inep_censo_educacao_superior.curso` c "
                      f"LEFT JOIN `basedosdados.br_inep_censo_educacao_superior.ies` i ON i.id_ies = c.id_ies AND i.ano = c.ano "
                      f"WHERE c.ano = 2024 AND c.id_municipio = '{VOTU}' AND c.quantidade_matriculas > 0 "
                      f"ORDER BY c.tipo_modalidade_ensino, c.quantidade_concluintes DESC LIMIT 40"):
        out(dict(r))


bloco("diretórios", diretorios)
bloco("dicionário censo escolar", dic_escolar)
bloco("dicionário censo superior", dic_superior)
bloco("turmas de Votuporanga 2024 por etapa", turmas_votu)
bloco("escolas de Votuporanga 2025", escolas_votu)
bloco("cursos de Votuporanga 2024", cursos_votu)

with open("diagnostico.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(linhas))
token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
with open("diagnostico.txt", "rb") as f:
    requests.post(f"https://api.telegram.org/bot{token}/sendDocument",
                  data={"chat_id": chat, "caption": "🎓 Radar Formaturas: diagnóstico 2. Mande este arquivo para o Claude."},
                  files={"document": ("diagnostico2-formaturas.txt", f, "text/plain")}, timeout=120)
