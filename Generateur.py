import streamlit as st
import pandas as pd
import time, logging, httpx, io
from datetime import datetime
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError

# ─── LOG ───────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ─── SECRETS NOTION ────────────────────────────────────
NOTION_API_KEY                  = st.secrets["notion_api_key"]
ID_RECETTES                     = st.secrets["notion_database_id_recettes"]
ID_MENUS                        = st.secrets["notion_database_id_menus"]
ID_INGREDIENTS                  = st.secrets["notion_database_id_ingredients"]
ID_INGREDIENTS_RECETTES         = st.secrets["notion_database_id_ingredients_recettes"]

notion = Client(auth=NOTION_API_KEY)

# ─── CONSTANTES EXPORT ─────────────────────────────────
BATCH = 50
RETRY = 3
WAIT  = 5

CSV_RECETTES            = "Recettes.csv"
CSV_MENUS               = "Menus.csv"
CSV_INGREDIENTS         = "Ingredients.csv"
CSV_INGREDIENTS_RECETTES= "Ingredients_recettes.csv"

# ─── PAGINATION COMMUNE ───────────────────────────────
def paginate(db_id, **kwargs):
    out, start, r = [], None, 0
    while True:
        try:
            resp = notion.databases.query(
                database_id=db_id,
                start_cursor=start,
                page_size=BATCH,
                **kwargs)
            out.extend(resp["results"])
            if not resp["has_more"]:
                break
            start = resp["next_cursor"]; time.sleep(0.3); r = 0
        except (RequestTimeoutError, httpx.TimeoutException):
            r += 1
            if r > RETRY: st.error("Timeout répété."); break
            time.sleep(WAIT * r)
        except APIResponseError as e:
            st.error(f"Erreur API Notion : {e}"); break
    return out

# ─── RECETTES (identique à votre version validée) ─────
#  … (gardez votre fonction extract_recettes existante) …

# ─── MENUS (identique à votre version validée) ────────
#  … (gardez extract_menus) …

# ─── INGREDIENTS ──────────────────────────────────────
HDR_INGR = ["Page_ID", "Nom", "Type de stock", "unité", "Qte reste"]
def extract_ingredients() -> pd.DataFrame:
    pages = paginate(
        ID_INGREDIENTS,
        filter={
            "property": "Type de stock",
            "select": {"equals": "Autre type"}  # même filtre que votre Colab
        })
    rows = []
    for p in pages:
        props = p["properties"]
        rows.append([
            p["id"],
            "".join(t["plain_text"] for t in props["Nom"]["title"]),
            props["Type de stock"]["select"]["name"] if props["Type de stock"]["select"] else "",
            "".join(t["plain_text"] for t in props["unité"]["rich_text"]),
            str(props["Qte reste"]["number"] or "")
        ])
    return pd.DataFrame(rows, columns=HDR_INGR)

# ─── INGREDIENTS ↔ RECETTES ───────────────────────────
HDR_IR = ["Page_ID", "Qté/pers_s", "Ingrédient ok", "Type de stock f"]
def extract_ingr_rec() -> pd.DataFrame:
    pages = paginate(
        ID_INGREDIENTS_RECETTES,
        filter={
            "property": "Type de stock f",
            "formula": {"string": {"equals": "Autre type"}}
        })
    rows = []
    for p in pages:
        props = p["properties"]
        parent_rel = props.get("Elément parent", {})
        parent_id = ""
        if parent_rel and parent_rel["type"] == "relation" and parent_rel["relation"]:
            parent_id = parent_rel["relation"][0]["id"]
        else:
            parent_id = p["id"]
        qte = props["Qté/pers_s"]["number"]
        if qte is not None and qte > 0:
            rows.append([
                parent_id,
                str(qte),
                ", ".join(r["id"] for r in props["Ingrédient ok"]["relation"]),
                props["Type de stock f"]["formula"]["string"] or ""
            ])
    return pd.DataFrame(rows, columns=HDR_IR)

# ─── INTERFACE STREAMLIT ──────────────────────────────
st.set_page_config(page_title="Exports Notion – 4 CSV", layout="centered")
st.title("📋 Exports Notion : Recettes, Menus, Ingrédients, Ingrédients-Recettes")

# Recettes
if st.button("Extraire les recettes"):
    with st.spinner("Extraction des recettes…"):
        df_r = extract_recettes()
    if df_r.empty:
        st.error("0 recette.")
    else:
        st.success(f"{len(df_r)} recettes.")
        st.dataframe(df_r, use_container_width=True)
        st.download_button("📥 Recettes.csv",
                           df_r.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
                           file_name=CSV_RECETTES, mime="text/csv")

st.divider()

# Menus
if st.button("Extraire les menus"):
    with st.spinner("Extraction des menus…"):
        df_m = extract_menus()
    if df_m.empty:
        st.error("0 menu.")
    else:
        st.success(f"{len(df_m)} menus.")
        st.dataframe(df_m, use_container_width=True)
        st.download_button("📥 Menus.csv",
                           df_m.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
                           file_name=CSV_MENUS, mime="text/csv")

st.divider()

# Ingrédients
if st.button("Extraire les ingrédients"):
    with st.spinner("Extraction des ingrédients…"):
        df_i = extract_ingredients()
    if df_i.empty:
        st.error("0 ingrédient.")
    else:
        st.success(f"{len(df_i)} ingrédients.")
        st.dataframe(df_i, use_container_width=True)
        st.download_button("📥 Ingredients.csv",
                           df_i.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
                           file_name=CSV_INGREDIENTS, mime="text/csv")

st.divider()

# Ingrédients ↔ Recettes
if st.button("Extraire ingrédients-recettes"):
    with st.spinner("Extraction des liens…"):
        df_ir = extract_ingr_rec()
    if df_ir.empty:
        st.error("0 lien.")
    else:
        st.success(f"{len(df_ir)} lignes.")
        st.dataframe(df_ir, use_container_width=True)
        st.download_button("📥 Ingredients_recettes.csv",
                           df_ir.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
                           file_name=CSV_INGREDIENTS_RECETTES, mime="text/csv")

st.info("Chaque bouton interroge uniquement la base concernée et génère un CSV conforme à vos modèles (UTF-8-SIG, mêmes en-têtes, même ordre).")
