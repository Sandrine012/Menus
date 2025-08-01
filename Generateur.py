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

# ──────────────────────────── CONFIG LOGGER ────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ──────────────────────────── CONSTANTES ───────────────────────────────
SAISON_FILTRE = "Printemps"
NUM_ROWS_TO_EXTRACT = 100_000
BATCH_SIZE = 50
MAX_RETRIES = 7
RETRY_DELAY_INITIAL = 10
API_TIMEOUT_SECONDS = 180

FICHIER_EXPORT_MENUS_CSV = "Menus.csv"
FICHIER_EXPORT_RECETTES_CSV = "Recettes.csv"
FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV = "Ingredients_recettes.csv"
FICHIER_EXPORT_INGREDIENTS_CSV = "Ingredients.csv"
FICHIER_EXPORT_GLOBAL_ZIP = "Notion_Exports.zip"

# ──────────────────────── CHARGEMENT DES SECRETS ───────────────────────
try:
    NOTION_API_KEY = st.secrets["notion_api_key"]
    DATABASE_ID_INGREDIENTS = st.secrets["notion_database_id_ingredients"]
    DATABASE_ID_INGREDIENTS_RECETTES = st.secrets["notion_database_id_ingredients_recettes"]
    DATABASE_ID_RECETTES = st.secrets["notion_database_id_recettes"]
    DATABASE_ID_MENUS = st.secrets["notion_database_id_menus"]
    notion = Client(auth=NOTION_API_KEY, timeout_ms=API_TIMEOUT_SECONDS * 1000)
except KeyError as e:
    st.error(f"Clé secrète manquante : {e}.")
    st.stop()

# ────────────────────────── FONCTIONS UTILITAIRE ───────────────────────
def parse_property_value(property_data):
    # … fonction identique à votre version (inchangée) …
    # pour gagner de l’espace, je ne la réaffiche pas ici
    pass

def query_notion_database(database_id, filter_obj=None, sort_obj=None, num_rows=NUM_ROWS_TO_EXTRACT):
    # … fonction identique à votre version …
    pass

def extract_dataframe_from_notion(database_id, column_mapping, filename_for_log=""):
    # … fonction identique à votre version …
    pass

@st.cache_data(ttl=3600, show_spinner="Extraction des menus…")
def get_menus_data():
    column_mapping = {"Nom Menu": "Nom",
                      "Recette": "Recette",
                      "Date": "Date"}
    df = extract_dataframe_from_notion(DATABASE_ID_MENUS, column_mapping, FICHIER_EXPORT_MENUS_CSV)
    if not df.empty:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
        df = df[["Nom Menu", "Recette", "Date"]]
    return df

@st.cache_data(ttl=3600, show_spinner="Extraction des recettes…")
def get_recipes_data():
    column_mapping = {
        "Nom": "Nom_plat",
        "ID_Recette": "ID_Recette",
        "Saison": "Saison",
        "Calories": "Calories Recette",
        "Proteines": "Proteines Recette",
        "Temps_total": "Temps_total",
        "Aime_pas_princip": "Aime_pas_princip",
        "Type_plat": "Type_plat",
        "Transportable": "Transportable",
    }
    df = extract_dataframe_from_notion(DATABASE_ID_RECETTES, column_mapping, FICHIER_EXPORT_RECETTES_CSV)
    for col in ["Calories", "Proteines", "Temps_total"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", "."), errors="coerce").fillna(0)
    if not df.empty:
        df = df[["Page_ID", "Nom", "ID_Recette", "Saison", "Calories",
                 "Proteines", "Temps_total", "Aime_pas_princip", "Type_plat",
                 "Transportable"]]
    return df

@st.cache_data(ttl=3600, show_spinner="Extraction ingrédients-recettes…")
def get_ingredients_recettes_data():
    column_mapping = {"Qté/pers_s": "Quantité/pers",
                      "Ingrédient ok": "Ingrédient",
                      "Type de stock f": "Type de stock"}
    df = extract_dataframe_from_notion(DATABASE_ID_INGREDIENTS_RECETTES, column_mapping,
                                       FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV)
    if "Qté/pers_s" in df.columns:
        df["Qté/pers_s"] = pd.to_numeric(df["Qté/pers_s"].astype(str).str.replace(",", "."),
                                         errors="coerce").fillna(0)
    if not df.empty:
        df = df[["Page_ID", "Qté/pers_s", "Ingrédient ok", "Type de stock f"]]
    return df

@st.cache_data(ttl=3600, show_spinner="Extraction des ingrédients…")
def get_ingredients_data():
    column_mapping = {"Nom": "Nom",
                      "Type de stock": "Type de stock",
                      "unité": "Unité",
                      "Qte reste": "Quantité restante"}
    df = extract_dataframe_from_notion(DATABASE_ID_INGREDIENTS, column_mapping, FICHIER_EXPORT_INGREDIENTS_CSV)
    if "Qte reste" in df.columns:
        df["Qte reste"] = pd.to_numeric(df["Qte reste"].astype(str).str.replace(",", "."),
                                        errors="coerce").fillna(0)
    if not df.empty:
        df = df[["Page_ID", "Nom", "Type de stock", "unité", "Qte reste"]]
    return df

def add_download_button(df: pd.DataFrame, filename: str):
    """Affiche le dataframe et ajoute un bouton de téléchargement CSV identique à l’exemple."""
    if df is None or df.empty:
        st.warning(f"Aucune donnée à afficher pour {filename}.")
        return
    st.dataframe(df, use_container_width=True)
    csv_bytes = df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button(
        label=f"⬇️ Télécharger {filename}",
        data=csv_bytes,
        file_name=filename,
        mime="text/csv",
    )

# ──────────────────────────── INTERFACE UI ─────────────────────────────
st.set_page_config(page_title="Générateur de Menus Notion", layout="centered")
st.title("🍽️ Générateur de Menus Automatisé avec Notion")

st.header("1. Vérification de la configuration")
st.info("Assurez-vous que vos clés API et IDs de bases Notion sont définis dans les *secrets* Streamlit.")

st.header("2. Exporter vos bases Notion")
st.write("Cliquez sur **Extraire tout** pour un ZIP contenant les quatre CSV, ou téléchargez chaque tableau individuellement.")

# --------- EXTRACTION INDIVIDUELLE ---------
with st.expander("Voir / télécharger chaque base individuellement", expanded=False):
    if st.button("Charger les données Notion"):
        menus = get_menus_data()
        recettes = get_recipes_data()
        ing_rec = get_ingredients_recettes_data()
        ingredients = get_ingredients_data()

        st.subheader("Menus")
        add_download_button(menus, FICHIER_EXPORT_MENUS_CSV)

        st.subheader("Recettes")
        add_download_button(recettes, FICHIER_EXPORT_RECETTES_CSV)

        st.subheader("Ingrédients ↔ Recettes")
        add_download_button(ing_rec, FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV)

        st.subheader("Ingrédients")
        add_download_button(ingredients, FICHIER_EXPORT_INGREDIENTS_CSV)

# --------- EXTRACTION GLOBALE ZIP ---------
st.markdown("### Export complet")
if st.button("Extraire tout"):
    csv_dict = {}
    with st.spinner("Extraction en cours…"):
        csv_dict[FICHIER_EXPORT_MENUS_CSV] = get_menus_data().to_csv(index=False, encoding="utf-8-sig")
        csv_dict[FICHIER_EXPORT_RECETTES_CSV] = get_recipes_data().to_csv(index=False, encoding="utf-8-sig")
        csv_dict[FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV] = get_ingredients_recettes_data().to_csv(index=False, encoding="utf-8-sig")
        csv_dict[FICHIER_EXPORT_INGREDIENTS_CSV] = get_ingredients_data().to_csv(index=False, encoding="utf-8-sig")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in csv_dict.items():
            zf.writestr(name, content.encode("utf-8-sig"))
    buf.seek(0)

    st.download_button(
        label=f"⬇️ Télécharger {FICHIER_EXPORT_GLOBAL_ZIP}",
        data=buf.getvalue(),
        file_name=FICHIER_EXPORT_GLOBAL_ZIP,
        mime="application/zip",
    )
    st.success("ZIP prêt !")

# ────────────────────────── FIN DU SCRIPT ─────────────────────────────
if __name__ == "__main__":
    pass
