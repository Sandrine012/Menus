import streamlit as st
import pandas as pd
import time, logging, httpx, io
from datetime import datetime
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError

# ───────────────────── CONFIG LOG ─────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ─────────── PARAMÈTRES NOTION ───────────
NOTION_API_KEY               = st.secrets["notion_api_key"]
DATABASE_ID_RECETTES         = st.secrets["notion_database_id_recettes"]
DATABASE_ID_MENUS            = st.secrets["notion_database_id_menus"]
SAISON_FILTRE                = "Printemps"       # identique au Colab

notion = Client(auth=NOTION_API_KEY)
logger.info("Client Notion initialisé")

# ─────────── CONSTANTES EXPORT ───────────
BATCH_SIZE         = 50
MAX_RETRIES        = 3
RETRY_DELAY_S      = 5
NUM_ROWS_TO_EXTRACT = 400          # pour les recettes
CSV_RECETTES       = "Recettes.csv"
CSV_MENUS          = "Menus.csv"

# ─────────── FILTRE RECETTES ───────────
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

# ─────────── EN-TÊTES & MAPPING RECETTES ───────────
HEADER_RECETTES = [
    "Page_ID", "Nom", "ID_Recette", "Saison",
    "Calories", "Proteines", "Temps_total",
    "Aime_pas_princip", "Type_plat", "Transportable",
]
CSV_TO_NOTION = {
    "Nom":              ("Nom_plat", "title"),
    "ID_Recette":       ("ID_Recette", "unique_id"),
    "Saison":           ("Saison", "multi_select"),
    "Calories":         ("Calories Recette", "rollup_number"),
    "Proteines":        ("Proteines Recette", "rollup_number"),
    "Temps_total":      ("Temps_total", "formula_number_or_string"),
    "Aime_pas_princip": ("Aime_pas_princip", "rollup_formula_string"),
    "Type_plat":        ("Type_plat", "multi_select"),
    "Transportable":    ("Transportable", "select_or_checkbox_oui"),
}

# ─────────── HELPER get_property_value ───────────
def get_property_value(prop, fmt):
    if not prop:
        return ""
    t = prop.get("type")
    try:
        if fmt == "title":
            return "".join(p.get("plain_text", "") for p in prop.get("title", []))
        if fmt == "unique_id":
            uid = prop["unique_id"]; p, n = uid.get("prefix"), uid.get("number")
            return f"{p}-{n}" if p and n is not None else str(n or "")
        if fmt == "multi_select":
            return ", ".join(o["name"] for o in prop.get("multi_select", []))
        if fmt in ("rollup_number", "formula_number_or_string"):
            if t == "rollup":
                num = prop["rollup"].get("number")
                if num is None and prop["rollup"].get("array"):
                    num = prop["rollup"]["array"][0].get("number")
                return str(num or "")
            if t == "formula":
                fo = prop["formula"]
                return str(fo.get("number") or fo.get("string") or "")
        if fmt == "rollup_formula_string":
            vals = [it["formula"].get("string") or "."
                    for it in prop.get("rollup", {}).get("array", [])
                    if it.get("type") == "formula"]
            return ", ".join(vals)
        if fmt == "select_or_checkbox_oui":
            if t == "select":
                return "Oui" if (prop["select"] or {}).get("name", "").lower() == "oui" else ""
            if t == "checkbox":
                return "Oui" if prop.get("checkbox") else ""
    except Exception as e:
        logger.error(f"Parsing error {fmt}: {e}")
    return ""

# ─────────── PAGINATION GÉNÉRIQUE ───────────
def paginate_query(db_id, **kwargs):
    out, start, retries = [], None, 0
    while True:
        try:
            resp = notion.databases.query(
                database_id=db_id,
                start_cursor=start,
                page_size=BATCH_SIZE,
                **kwargs)
            out.extend(resp["results"])
            if not resp["has_more"]:
                break
            start = resp["next_cursor"]
            time.sleep(0.3)
            retries = 0
        except (RequestTimeoutError, httpx.TimeoutException, httpx.ReadTimeout):
            retries += 1
            if retries > MAX_RETRIES:
                st.error("Timeout répété – abandon.")
                break
            time.sleep(RETRY_DELAY_S * retries)
        except APIResponseError as e:
            st.error(f"Erreur API Notion : {e}")
            break
    return out

# ─────────── EXTRACTION RECETTES ───────────
@st.cache_data(ttl=3_600)
def extract_recettes() -> pd.DataFrame:
    pages = paginate_query(DATABASE_ID_RECETTES, filter=FILTER_RECETTES)
    rows = []
    for p in pages[:NUM_ROWS_TO_EXTRACT]:
        props = p["properties"]
        row = [p["id"]]
        for col in HEADER_RECETTES[1:]:
            notion_key, fmt = CSV_TO_NOTION[col]
            row.append(get_property_value(props.get(notion_key), fmt))
        rows.append(row)
    return pd.DataFrame(rows, columns=HEADER_RECETTES)

# ─────────── EXTRACTION MENUS ───────────
HEADER_MENUS = ["Nom Menu", "Recette", "Date"]

@st.cache_data(ttl=3_600)
def extract_menus() -> pd.DataFrame:
    pages = paginate_query(
        DATABASE_ID_MENUS,
        filter={"property": "Recette", "relation": {"is_not_empty": True}},
    )
    rows = []
    for p in pages:
        props = p["properties"]

        # Nom Menu
        nom = "".join(t.get("plain_text", "") for t in props["Nom Menu"]["title"])

        # Recette IDs
        recette_ids = []
        rel_prop = props["Recette"]
        if rel_prop["type"] == "relation":
            recette_ids = [r["id"] for r in rel_prop["relation"]]
        elif rel_prop["type"] == "rollup":
            for it in rel_prop["rollup"].get("array", []):
                if it.get("id"):
                    recette_ids.append(it["id"])
                elif it.get("relation"):
                    recette_ids.extend(r["id"] for r in it["relation"])

        # Date
        date_val = ""
        date_prop = props["Date"]["date"]
        if date_prop and date_prop.get("start"):
            date_val = datetime.fromisoformat(
                date_prop["start"].replace("Z", "+00:00")
            ).strftime("%Y-%m-%d")

        rows.append([nom.strip(), ", ".join(recette_ids), date_val])

    return pd.DataFrame(rows, columns=HEADER_MENUS)

# ─────────── UI STREAMLIT ───────────
st.set_page_config(page_title="Exports Notion", layout="centered")
st.title("📋 Exports Notion : Recettes & Menus")

# -------- BOUTON RECETTES --------
if st.button("Extraire les recettes"):
    with st.spinner("Extraction des recettes…"):
        df_r = extract_recettes()

    if df_r.empty:
        st.error("Aucune recette trouvée.")
    else:
        st.success(f"{len(df_r)} recettes extraites.")
        st.dataframe(df_r, use_container_width=True)

        csv_bytes = df_r.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("📥 Télécharger Recettes.csv",
                           data=csv_bytes,
                           file_name=CSV_RECETTES,
                           mime="text/csv")

st.divider()

# -------- BOUTON MENUS --------
if st.button("Extraire les menus"):
    with st.spinner("Extraction des menus…"):
        df_m = extract_menus()

    if df_m.empty:
        st.error("Aucun menu trouvé (vérifiez l’ID ou les permissions).")
    else:
        st.success(f"{len(df_m)} menus extraits.")
        st.dataframe(df_m, use_container_width=True)

        csv_bytes = df_m.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("📥 Télécharger Menus.csv",
                           data=csv_bytes,
                           file_name=CSV_MENUS,
                           mime="text/csv")

st.info("Chaque extraction est indépendante : vous pouvez exporter Recettes ou Menus séparément.")
