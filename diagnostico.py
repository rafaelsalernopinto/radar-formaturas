"""
Radar Formaturas: diagnóstico.
Descobre a estrutura das tabelas de educação na Base dos Dados (BigQuery)
e manda um relatório em texto pelo Telegram. Não gera lista ainda.
"""
import json
import os

import requests
from google.cloud import bigquery
from google.oauth2 import service_account

DATASETS = ["basedosdados.br_inep_censo_escolar", "basedosdados.br_inep_censo_educacao_superior"]
MUNICIPIO = "basedosdados.br_bd_diretorios_brasil.municipio"
LIMITE = 50 * 10**9
linhas = []


def out(*a):
    texto = " ".join(str(x) for x in a)
    print(texto, flush=True)
    linhas.append(texto)


info = json.loads(os.environ["GCP_KEY"])
bq = bigquery.Client(credentials=service_account.Credentials.from_service_account_info(info),
                     project=info["project_id"])


def consulta(sql):
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=LIMITE)
    return list(bq.query(sql, job_config=cfg).result())


# 1. Diretório de municípios: colunas e microrregiões da área
try:
    cols = [c.name for c in bq.get_table(MUNICIPIO).schema]
    out("== municipio:", ", ".join(cols))
    col_micro = next((c for c in cols if "microrregiao" in c and "nome" in c), None)
    if col_micro:
        for r in consulta(f"SELECT {col_micro} AS m, COUNT(*) n, STRING_AGG(nome, ', ') cidades FROM `{MUNICIPIO}` "
                          f"WHERE sigla_uf='SP' AND ({col_micro} LIKE '%Votuporanga%' OR {col_micro} LIKE '%Fernand%' "
                          f"OR {col_micro} LIKE '%Jales%') GROUP BY 1"):
            out(f"MICRO {r.m} ({r.n}): {r.cidades}")
        for r in consulta(f"SELECT id_municipio, nome FROM `{MUNICIPIO}` WHERE sigla_uf='SP' AND nome='Votuporanga'"):
            out("VOTUPORANGA id:", r.id_municipio)
except Exception as e:
    out("ERRO municipio:", e)

# 2. Tabelas e colunas das bases de educação
for ds in DATASETS:
    try:
        tabelas = [t.table_id for t in bq.list_tables(ds)]
        out(f"\n== {ds}: {', '.join(tabelas)}")
        for t in tabelas:
            try:
                tb = bq.get_table(f"{ds}.{t}")
                out(f"-- {t} ({tb.num_rows} linhas): " + ", ".join(f"{c.name}:{c.field_type}" for c in tb.schema))
            except Exception as e:
                out(f"-- {t}: ERRO {e}")
    except Exception as e:
        out(f"ERRO {ds}:", e)

# 3. Anos disponíveis e amostras de Votuporanga (id 3557105)
for ds, t in [("basedosdados.br_inep_censo_escolar", "turma"), ("basedosdados.br_inep_censo_escolar", "escola"),
              ("basedosdados.br_inep_censo_educacao_superior", "curso"),
              ("basedosdados.br_inep_censo_educacao_superior", "ies")]:
    try:
        cols = [c.name for c in bq.get_table(f"{ds}.{t}").schema]
        if "ano" in cols:
            anos = [r.ano for r in consulta(f"SELECT DISTINCT ano FROM `{ds}.{t}` ORDER BY ano DESC LIMIT 3")]
            out(f"\nANOS {t}: {anos}")
            filtro = f"ano = {anos[0]}"
        else:
            filtro = "TRUE"
        if "id_municipio" in cols:
            filtro += " AND id_municipio = '3557105'"
        for r in consulta(f"SELECT * FROM `{ds}.{t}` WHERE {filtro} LIMIT 3"):
            out(f"AMOSTRA {t}: " + json.dumps({k: str(v) for k, v in dict(r).items()}, ensure_ascii=False)[:1500])
    except Exception as e:
        out(f"ERRO amostra {t}:", e)

# 4. Envia o relatório como arquivo
with open("diagnostico.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(linhas))
token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
with open("diagnostico.txt", "rb") as f:
    requests.post(f"https://api.telegram.org/bot{token}/sendDocument",
                  data={"chat_id": chat, "caption": "🎓 Radar Formaturas: diagnóstico. Mande este arquivo para o Claude."},
                  files={"document": ("diagnostico-formaturas.txt", f, "text/plain")}, timeout=120)
