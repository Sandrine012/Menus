import streamlit as st
import pandas as pd
import logging
import time
import httpx
import io
import zipfile
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError
from datetime import datetime

# ───────────────────────── CONFIG LOGGER ──────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ────────────────────────── CONSTANTES ────────────────────────────
SAISON_FILTRE = "Printemps"
NUM_ROWS_TO_EXTRACT = 100_000
BATCH_SIZE = 50
MAX_RETRIES = 7
RETRY_DELAY_INITIAL = 10

FICHIER_EXPORT_MENUS_CSV = "Menus.csv"
FICHIER_EXPORT_RECETTES_CSV = "Recettes.csv"
FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV = "Ingredients_recettes.csv"
FICHIER_EXPORT_INGREDIENTS_CSV = "Ingredients.csv"
FICHIER_EXPORT_GLOBAL_ZIP = "Notion_Exports.zip"

# ──────────────────────── CHARGEMENT SECRETS ──────────────────────
try:
    NOTION_API_KEY = st.secrets["notion_api_key"]
    DATABASE_ID_INGREDIENTS = st.secrets["notion_database_id_ingredients"]
    DATABASE_ID_INGREDIENTS_RECETTES = st.secrets["notion_database_id_ingredients_recettes"]
    DATABASE_ID_RECETTES = st.secrets["notion_database_id_recettes"]
    DATABASE_ID_MENUS = st.secrets["notion_database_id_menus"]
    notion = Client(auth=NOTION_API_KEY)
    logger.info("Client Notion initialisé.")
except Exception as e:
    st.error(f"Erreur de configuration Notion : {e}")
    st.stop()

# ──────────────────────── UTILITAIRES NOTION ──────────────────────
def get_property_value(prop_data, expected_type):
    """Extrait proprement la valeur d’une propriété Notion."""
    if not prop_data:
        return ""
    t = prop_data.get("type")
    try:
        if expected_type == "title":
            return "".join(tk.get("plain_text", "") for tk in prop_data.get("title", []))
        if expected_type == "rich_text":
            return "".join(tk.get("plain_text", "") for tk in prop_data.get("rich_text", []))
        if expected_type == "number":
            return prop_data.get("number") or ""
        if expected_type == "multi_select":
            return ", ".join(o.get("name", "") for o in prop_data.get("multi_select", []))
        if expected_type == "select_to_oui":
            if t == "select":
                return "Oui" if prop_data.get("select", {}).get("name", "").lower() == "oui" else ""
            if t == "checkbox":
                return "Oui" if prop_data.get("checkbox") else ""
            return ""
        if expected_type == "unique_id":
            uid = prop_data.get("unique_id", {})
            p, n = uid.get("prefix"), uid.get("number")
            return f"{p}-{n}" if p and n is not None else str(n or "")
        if expected_type == "relation_id":
            rels = prop_data.get("relation", [])
            return rels[0]["id"] if rels else ""
        if expected_type == "date":
            d = prop_data.get("date", {})
            return d.get("start", "")
        if expected_type == "rollup_number":
            ro = prop_data.get("rollup", {})
            if ro.get("type") == "number":
                return ro.get("number") or ""
            arr = ro.get("array", [])
            if arr and arr[0].get("type") == "number":
                return arr[0].get("number") or ""
            return ""
        if expected_type == "rollup_formula_string":
            vals = []
            for item in prop_data.get("rollup", {}).get("array", []):
                if item.get("type") == "formula":
                    vals.append(item.get("formula", {}).get("string") or ".")
            return ", ".join(vals)
    except Exception as exc:
        logger.error(f"Erreur de parsing propriété Notion : {exc}")
    return ""

def fetch_database(db_id, rows=NUM_ROWS_TO_EXTRACT, filter_obj=None):
    """Interroge Notion avec gestion simple du rate-limit."""
    out, start, retries = [], None, 0
    while len(out) < rows:
        try:
            resp = notion.databases.query(
                database_id=db_id,
                page_size=BATCH_SIZE,
                start_cursor=start,
                filter=filter_obj)
            out.extend(resp["results"])
            if not resp["has_more"]:
                break
            start = resp["next_cursor"]
            retries = 0
        except (RequestTimeoutError, httpx.TimeoutException):
            retries += 1
            if retries > MAX_RETRIES:
                logger.error(f"Timeout répété sur {db_id}")
                break
            time.sleep(RETRY_DELAY_INITIAL * retries)
        except APIResponseError as api_err:
            logger.error(f"API Notion error {api_err}")
            break
    return out[:rows]

def build_df(pages, mapping, header):
    """Convertit les pages Notion en DataFrame selon le mapping voulu."""
    rows = []
    for p in pages:
        d = {}
        props = p.get("properties", {})
        for csv_col, (notion_prop, expected_type) in mapping.items():
            if csv_col == "Page_ID":
                d[csv_col] = p.get("id", "")
            else:
                d[csv_col] = get_property_value(props.get(notion_prop), expected_type)
        rows.append(d)
    df = pd.DataFrame(rows)
    # garantir toutes les colonnes dans l’ordre voulu
    for col in header:
        if col not in df.columns:
            df[col] = ""
    return df[header]

# ────────────────────────── MAPPINGS CSV ───────────────────────────
map_recettes = {
    "Page_ID":               (None, "page_id"),
    "Nom":                   ("Nom_plat", "title"),
    "ID_Recette":            ("ID_Recette", "unique_id"),
    "Saison":                ("Saison", "multi_select"),
    "Calories":              ("Calories Recette", "rollup_number"),
    "Proteines":             ("Proteines Recette", "rollup_number"),
    "Temps_total":           ("Temps_total", "number"),
    "Aime_pas_princip":      ("Aime_pas_princip", "rollup_formula_string"),
    "Type_plat":             ("Type_plat", "multi_select"),
    "Transportable":         ("Transportable", "select_to_oui"),
}
header_recettes = list(map_recettes.keys())

map_menus = {
    "Page_ID":   (None, "page_id"),
    "Nom Menu":  ("Nom Menu", "title"),
    "Recette":   ("Recette", "relation_id"),
    "Date":      ("Date", "date"),
}
header_menus = list(map_menus.keys())

map_ingredients = {
    "Page_ID":      (None, "page_id"),
    "Nom":          ("Nom", "title"),
    "Type de stock":("Type de stock", "select_to_oui"),
    "unité":        ("unité", "rich_text"),
    "Qte reste":    ("Qté reste", "number"),
}
header_ingredients = list(map_ingredients.keys())

map_ing_rec = {
    "Page_ID":         (None, "page_id"),
    "Qté/pers_s":      ("Qté/pers_s", "number"),
    "Ingrédient ok":   ("Ingrédient ok", "relation_id"),
    "Type de stock f": ("Type de stock f", "select_to_oui"),
}
header_ing_rec = list(map_ing_rec.keys())

# ──────────────────────── EXTRACT FUNCTIONS ───────────────────────
@st.cache_data(ttl=3_600, show_spinner="Extraction Recettes…")
def get_recettes_df():
    pages = fetch_database(DATABASE_ID_RECETTES)
    df = build_df(pages, map_recettes, header_recettes)
    num_cols = ["Calories", "Proteines", "Temps_total"]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c].astype(str).str.replace(",", "."), errors="coerce").fillna(0)
    return df

@st.cache_data(ttl=3_600, show_spinner="Extraction Menus…")
def get_menus_df():
    pages = fetch_database(DATABASE_ID_MENUS)
    df = build_df(pages, map_menus, header_menus)
    if not df.empty:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return df

@st.cache_data(ttl=3_600, show_spinner="Extraction Ingrédients…")
def get_ingredients_df():
    pages = fetch_database(DATABASE_ID_INGREDIENTS)
    df = build_df(pages, map_ingredients, header_ingredients)
    df["Qte reste"] = pd.to_numeric(df["Qte reste"].astype(str).str.replace(",", "."), errors="coerce").fillna(0)
    return df

@st.cache_data(ttl=3_600, show_spinner="Extraction Ingrédients-Recettes…")
def get_ing_rec_df():
    pages = fetch_database(DATABASE_ID_INGREDIENTS_RECETTES)
    df = build_df(pages, map_ing_rec, header_ing_rec)
    df["Qté/pers_s"] = pd.to_numeric(df["Qté/pers_s"].astype(str).str.replace(",", "."), errors="coerce").fillna(0)
    return df

# ──────────────────────── UI HELPERS ───────────────────────────────
def add_csv_download(df, filename):
    """Affiche df et fournit un bouton de téléchargement CSV."""
    if df.empty:
        st.warning(f"Aucune donnée pour {filename}.")
        return
    st.dataframe(df, use_container_width=True)
    csv_bytes = df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button(f"⬇️ Télécharger {filename}", csv_bytes, filename, "text/csv")

# ───────────────────────── INTERFACE UI ────────────────────────────
st.set_page_config(page_title="Générateur de Menus Notion", layout="centered")
st.title("🍽️ Générateur de Menus Automatisé – Notion")

st.header("1. Charger / rafraîchir les données Notion")
if st.button("Charger les 4 bases"):
    st.session_state["recettes"] = get_recettes_df()
    st.session_state["menus"] = get_menus_df()
    st.session_state["ingredients"] = get_ingredients_df()
    st.session_state["ing_rec"] = get_ing_rec_df()
    st.success("Données chargées.")

# Affichage + téléchargement individuel
if "recettes" in st.session_state:
    st.subheader("Recettes")
    add_csv_download(st.session_state["recettes"], FICHIER_EXPORT_RECETTES_CSV)

    st.subheader("Menus")
    add_csv_download(st.session_state["menus"], FICHIER_EXPORT_MENUS_CSV)

    st.subheader("Ingrédients")
    add_csv_download(st.session_state["ingredients"], FICHIER_EXPORT_INGREDIENTS_CSV)

    st.subheader("Ingrédients ↔ Recettes")
    add_csv_download(st.session_state["ing_rec"], FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV)

# ───── ZIP rapide basé sur session_state ─────
if all(k in st.session_state for k in ("recettes", "menus", "ingredients", "ing_rec")):
    if st.button("⬇️ Télécharger ZIP des données chargées"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(FICHIER_EXPORT_RECETTES_CSV,
                        st.session_state["recettes"].to_csv(index=False, encoding="utf-8-sig"))
            zf.writestr(FICHIER_EXPORT_MENUS_CSV,
                        st.session_state["menus"].to_csv(index=False, encoding="utf-8-sig"))
            zf.writestr(FICHIER_EXPORT_INGREDIENTS_CSV,
                        st.session_state["ingredients"].to_csv(index=False, encoding="utf-8-sig"))
            zf.writestr(FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV,
                        st.session_state["ing_rec"].to_csv(index=False, encoding="utf-8-sig"))
        buf.seek(0)
        st.download_button(f"⬇️ Télécharger {FICHIER_EXPORT_GLOBAL_ZIP}",
                           buf.getvalue(),
                           FICHIER_EXPORT_GLOBAL_ZIP,
                           "application/zip")

# ─────────────────────────── FIN ────────────────────────────
if __name__ == "__main__":
    pass
