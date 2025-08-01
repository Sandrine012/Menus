import streamlit as st
import pandas as pd
import time, logging, httpx, io, csv
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError

# ───────────────────── CONFIG LOG ─────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ─────────────────── PARAMÈTRES NOTION ─────────────────
NOTION_API_KEY = st.secrets["notion_api_key"]
DATABASE_ID    = st.secrets["notion_database_id_recettes"]
SAISON_FILTRE  = "Printemps"        # identique au Colab

notion = Client(auth=NOTION_API_KEY)
logger.info("Client Notion initialisé")

# ─────────────── CONST EXPORT / PAGINATION ─────────────
CSV_NAME            = "Recettes.csv"
NUM_ROWS_TO_EXTRACT = 400
BATCH_SIZE          = 50
MAX_RETRIES         = 3
RETRY_DELAY_S       = 5

# ─────────────── FILTRE NOTION (copié) ────────────────
filter_conditions = [
    {"property": "Elément parent", "relation": {"is_empty": True}},
    {"or": [
        {"property": "Saison", "multi_select": {"contains": "Toute l'année"}},
        *([{"property": "Saison", "multi_select": {"contains": SAISON_FILTRE}}] if SAISON_FILTRE else []),
        {"property": "Saison", "multi_select": {"is_empty": True}},
    ]},
    {"or": [
        {"property": "Type_plat", "multi_select": {"contains": "Salade"}},
        {"property": "Type_plat", "multi_select": {"contains": "Soupe"}},
        {"property": "Type_plat", "multi_select": {"contains": "Plat"}},
    ]},
]
FILTER_RECETTES = {"and": filter_conditions}

# ─────────────── MAPPING & EN-TÊTES CSV ───────────────
HEADER_CSV = [
    "Page_ID", "Nom", "ID_Recette", "Saison",
    "Calories", "Proteines", "Temps_total",
    "Aime_pas_princip", "Type_plat", "Transportable",
]

CSV_TO_NOTION = {
    "Page_ID":          (None, "page_id_special"),
    "Nom":              ("Nom_plat", "title"),
    "ID_Recette":       ("ID_Recette", "unique_id_or_text"),
    "Saison":           ("Saison", "multi_select_comma_separated"),
    "Calories":         ("Calories Recette", "rollup_single_number_or_empty"),
    "Proteines":        ("Proteines Recette", "rollup_single_number_or_empty"),
    "Temps_total":      ("Temps_total", "formula_number_or_string_or_empty"),
    "Aime_pas_princip": ("Aime_pas_princip", "rollup_formula_string_dots_comma_separated"),
    "Type_plat":        ("Type_plat", "multi_select_comma_separated"),
    "Transportable":    ("Transportable", "select_to_oui_empty"),
}

# ──────────── HELPER get_property_value ────────────
def get_property_value(prop_data, _, fmt):
    if not prop_data:
        return ""
    t = prop_data.get("type")
    try:
        if fmt == "title":
            return "".join(p.get("text", {}).get("content", "") for p in prop_data.get("title", []))
        if fmt == "unique_id_or_text":
            if t == "unique_id":
                uid = prop_data["unique_id"]; p, n = uid.get("prefix"), uid.get("number")
                return f"{p}-{n}" if p and n is not None else str(n or "")
            if t == "title":
                return get_property_value(prop_data, _ , "title")
            if t == "rich_text":
                return get_property_value(prop_data, _ , "rich_text_plain")
        if fmt == "rich_text_plain":
            return "".join(p.get("plain_text", "") for p in prop_data.get("rich_text", []))
        if fmt == "multi_select_comma_separated":
            return ", ".join(o.get("name", "") for o in prop_data.get("multi_select", []))
        if fmt == "number_to_string_or_empty":
            return str(prop_data.get("number") or "")
        if fmt == "formula_number_or_string_or_empty":
            fo = prop_data.get("formula", {}); ft = fo.get("type")
            return str(fo.get("number") or "") if ft == "number" else fo.get("string", "")
        if fmt == "rollup_single_number_or_empty":
            ro = prop_data.get("rollup", {}); rt = ro.get("type")
            if rt == "number":
                return str(ro.get("number") or "")
            if rt == "array" and ro["array"]:
                item = ro["array"][0]
                if item["type"] == "number":
                    return str(item["number"] or "")
                if item["type"] == "formula":
                    return str(item["formula"].get("number") or "")
        if fmt == "rollup_formula_string_dots_comma_separated":
            vals = []
            for it in prop_data.get("rollup", {}).get("array", []):
                if it.get("type") == "formula":
                    s = it["formula"].get("string") or "."
                    vals.append(s if s.strip() else ".")
                else:
                    vals.append(".")
            return ", ".join(vals)
        if fmt == "select_to_oui_empty":
            if t == "select":
                return "Oui" if (prop_data["select"] or {}).get("name", "").lower() == "oui" else ""
            if t == "checkbox":
                return "Oui" if prop_data.get("checkbox") else ""
    except Exception as e:
        logger.error(f"Parsing error {fmt}: {e}")
    return ""

# ──────────── EXTRACTION (une fonction) ────────────
@st.cache_data(show_spinner="Extraction des recettes…", ttl=3_600)
def extract_recettes() -> pd.DataFrame:
    out, start, retries = [], None, 0
    while len(out) < NUM_ROWS_TO_EXTRACT:
        try:
            resp = notion.databases.query(
                database_id=DATABASE_ID,
                filter=FILTER_RECETTES,
                page_size=BATCH_SIZE,
                start_cursor=start,
            )
            out.extend(resp["results"])
            if not resp["has_more"]:
                break
            start = resp["next_cursor"]
            time.sleep(0.3)  # pause courte pour respecter le rate-limit
            retries = 0
        except (RequestTimeoutError, httpx.TimeoutException, httpx.ReadTimeout):
            retries += 1
            if retries > MAX_RETRIES:
                st.error("Timeout répété – abandon.")
                break
            time.sleep(RETRY_DELAY_S * retries)
        except APIResponseError as e:
            st.error(f"Erreur API Notion : {e.message}")
            break

    # conversion vers DataFrame
    rows = []
    for p in out:
        props = p["properties"]
        row = []
        for col in HEADER_CSV:
            if col == "Page_ID":
                row.append(p["id"])
            else:
                notion_key, fmt = CSV_TO_NOTION[col]
                row.append(get_property_value(props.get(notion_key), notion_key, fmt))
        rows.append(row)

    df = pd.DataFrame(rows, columns=HEADER_CSV)
    return df

# ──────────────── INTERFACE STREAMLIT ────────────────
st.set_page_config(layout="centered", page_title="Export Recettes Notion")
st.title("📋 Export des Recettes – Notion → CSV")

st.markdown("Cliquez pour extraire les recettes de Notion puis télécharger le CSV.")

if st.button("Extraire les recettes"):
    df = extract_recettes()
    if df.empty:
        st.error("Aucune recette correspondant au filtre.")
    else:
        st.success(f"{len(df)} recettes extraites.")
        st.dataframe(df, use_container_width=True)

        # —— bouton de téléchargement CSV —— 
        csv_bytes = df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button(
            "📥 Télécharger Recettes.csv",
            data=csv_bytes,
            file_name=CSV_NAME,
            mime="text/csv",
        )

st.info("Les en-têtes, l’ordre des colonnes et l’encodage sont identiques à votre fichier d’origine.")
