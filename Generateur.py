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

# ───────────── CONFIGURATION LOGGER ─────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ─────────── CONSTANTES ET NOMS DE FICHIERS ───────────
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

# ─────────── CONNEXION NOTION ───────────
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

# ─────────── UTILITAIRES (inchangés) ───────────
def get_property_value(prop_data, notion_prop_name_for_log, expected_format_key):
    # … contenu identique à votre version précédente …
    # (la fonction complète reste inchangée)
    if not prop_data:
        return ""
    prop_type = prop_data.get("type")
    try:
        if expected_format_key == "title":
            return "".join(t.get("text", {}).get("content", "") for t in prop_data.get("title", []))
        # ——— toutes vos autres branches if/elif restent telles quelles ———
    except Exception as e:
        logger.error(f"EXC Formatage: '{notion_prop_name_for_log}' ({expected_format_key}): {e}")
        return "ERREUR_FORMAT"
    return ""

def fetch_data_from_notion(database_id, num_rows, filter_conditions=None):
    results, has_more, start_cursor, retries = [], True, None, 0
    query_payload = {"page_size": BATCH_SIZE}
    if filter_conditions:
        query_payload["filter"] = filter_conditions
    while has_more and len(results) < num_rows and retries < MAX_RETRIES:
        try:
            if start_cursor:
                query_payload["start_cursor"] = start_cursor
            resp = notion.databases.query(database_id=database_id, **query_payload)
            results.extend(resp["results"])
            has_more = resp["has_more"]
            start_cursor = resp["next_cursor"]
            retries = 0
            if has_more and len(results) < num_rows:
                time.sleep(RETRY_DELAY_INITIAL)
        except (RequestTimeoutError, httpx.TimeoutException, httpx.ReadTimeout):
            retries += 1
            sleep_s = RETRY_DELAY_INITIAL * retries
            logger.warning(f"Timeout Notion, nouvelle tentative dans {sleep_s}s…")
            time.sleep(sleep_s)
        except APIResponseError as api_err:
            logger.error(f"API Notion error : {api_err}")
            retries += 1
            time.sleep(RETRY_DELAY_INITIAL * retries)
        except Exception as err:
            logger.error(f"Erreur inattendue : {err}")
            retries += 1
            time.sleep(RETRY_DELAY_INITIAL * retries)
    if retries >= MAX_RETRIES:
        logger.error(f"Échec d’extraction après {MAX_RETRIES} tentatives.")
    return results[:num_rows]

def process_notion_pages_to_dataframe(pages, mapping, header):
    rows = []
    for page in pages:
        d = {}
        props = page.get("properties", {})
        for csv_col, (notion_prop, fmt_key) in mapping.items():
            if csv_col == "Page_ID":
                d[csv_col] = page.get("id", "")
            else:
                d[csv_col] = get_property_value(props.get(notion_prop), notion_prop, fmt_key)
        rows.append(d)
    df = pd.DataFrame(rows)
    for col in header:               # garantit toutes les colonnes
        if col not in df.columns:
            df[col] = ""
    return df[header]

# ─────────── MAPPINGS (inchangés) ───────────
mapping_recipes = {
    "Page_ID": (None, "page_id_special"),
    "Nom": ("Nom_plat", "title"),
    "ID_Recette": ("ID_Recette", "unique_id_or_text"),
    "Saison": ("Saison", "multi_select_comma_separated"),
    "Calories": ("Calories Recette", "rollup_single_number_or_empty"),
    "Proteines": ("Proteines Recette", "rollup_single_number_or_empty"),
    "Temps_total": ("Temps_total", "formula_number_or_string_or_empty"),
    "Aime_pas_princip": ("Aime_pas_princip", "rollup_formula_string_dots_comma_separated"),
    "Type_plat": ("Type_plat", "multi_select_comma_separated"),
    "Transportable": ("Transportable", "select_to_oui_empty"),
}
header_recipes = list(mapping_recipes.keys())

mapping_menus = {
    "Page_ID": (None, "page_id_special"),
    "Nom Menu": ("Nom Menu", "title"),
    "Recette": ("Recette", "relation_id_or_empty"),
    "Date": ("Date", "date_start_or_empty"),
}
header_menus = list(mapping_menus.keys())

mapping_ingredients = {
    "Page_ID": (None, "page_id_special"),
    "Nom": ("Nom", "title"),
    "Type de stock": ("Type de stock", "select_to_oui_empty"),
    "unité": ("unité", "rich_text_plain"),
    "Qte reste": ("Qté reste", "number_to_string_or_empty"),
}
header_ingredients = list(mapping_ingredients.keys())

mapping_ingredients_recettes = {
    "Page_ID": (None, "page_id_special"),
    "Qté/pers_s": ("Qté/pers_s", "number_to_string_or_empty"),
    "Ingrédient ok": ("Ingrédient ok", "relation_id_or_empty"),
    "Type de stock f": ("Type de stock f", "select_to_oui_empty"),
}
header_ingredients_recettes = list(mapping_ingredients_recettes.keys())

# ─────────── FONCTIONS D’EXTRACTION INDIVIDUELLES ───────────
def get_notion_recipes_data():
    filt = {
        "and": [
            {"property": "Elément parent", "relation": {"is_empty": True}},
            {"or": [
                {"property": "Saison", "multi_select": {"contains": "Toute l'année"}},
                {"property": "Saison", "multi_select": {"contains": SAISON_FILTRE}},
                {"property": "Saison", "multi_select": {"is_empty": True}},
            ]},
            {"or": [
                {"property": "Type_plat", "multi_select": {"contains": "Salade"}},
                {"property": "Type_plat", "multi_select": {"contains": "Soupe"}},
                {"property": "Type_plat", "multi_select": {"contains": "Plat"}},
            ]},
        ]
    }
    pages = fetch_data_from_notion(DATABASE_ID_RECETTES, NUM_ROWS_TO_EXTRACT, filt)
    return process_notion_pages_to_dataframe(pages, mapping_recipes, header_recipes)

def get_notion_ingredients_data():
    pages = fetch_data_from_notion(DATABASE_ID_INGREDIENTS, NUM_ROWS_TO_EXTRACT)
    return process_notion_pages_to_dataframe(pages, mapping_ingredients, header_ingredients)

def get_notion_ingredients_recipes_data():
    pages = fetch_data_from_notion(DATABASE_ID_INGREDIENTS_RECETTES, NUM_ROWS_TO_EXTRACT)
    return process_notion_pages_to_dataframe(pages, mapping_ingredients_recettes, header_ingredients_recettes)

def get_existing_menus_data():
    pages = fetch_data_from_notion(DATABASE_ID_MENUS, NUM_ROWS_TO_EXTRACT)
    return process_notion_pages_to_dataframe(pages, mapping_menus, header_menus)

# ─────────── INTERFACE STREAMLIT ───────────
st.set_page_config(layout="wide")
st.title("Application de Génération de Menus Notion")

st.header("1. Vérification")
st.success("Connexion Notion OK. Secrets chargés.")

# -------------- BOUTON D’EXTRACTION ORIGINALE --------------
st.header("2. Extraction complète (API Notion)")
if st.button("Extraire et Télécharger Toutes les Données de Notion"):
    csv_dict, extraction_successful = {}, True

    with st.spinner("Recettes…"):
        df_recettes = get_notion_recipes_data()
        if not df_recettes.empty:
            csv_dict[FICHIER_EXPORT_RECETTES_CSV] = df_recettes.to_csv(index=False, encoding="utf-8-sig")
            st.success(f"{len(df_recettes)} recettes.")
        else:
            st.error("Aucune recette."); extraction_successful = False

    with st.spinner("Ingrédients…"):
        df_ingredients = get_notion_ingredients_data()
        if not df_ingredients.empty:
            csv_dict[FICHIER_EXPORT_INGREDIENTS_CSV] = df_ingredients.to_csv(index=False, encoding="utf-8-sig")
            st.success(f"{len(df_ingredients)} ingrédients.")
        else:
            st.error("Aucun ingrédient."); extraction_successful = False

    with st.spinner("Ingrédients ↔ Recettes…"):
        df_ing_rec = get_notion_ingredients_recipes_data()
        if not df_ing_rec.empty:
            csv_dict[FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV] = df_ing_rec.to_csv(index=False, encoding="utf-8-sig")
            st.success(f"{len(df_ing_rec)} liens.")
        else:
            st.error("Aucun lien."); extraction_successful = False

    with st.spinner("Menus existants…"):
        df_menus = get_existing_menus_data()
        if not df_menus.empty:
            csv_dict[FICHIER_EXPORT_MENUS_CSV] = df_menus.to_csv(index=False, encoding="utf-8-sig")
            st.success(f"{len(df_menus)} menus.")
        else:
            st.error("Aucun menu."); extraction_successful = False

    if extraction_successful:
        st.session_state["csv_dict"] = csv_dict
        st.success("Extraction terminée – utilisez maintenant le bouton ci-dessous pour récupérer le ZIP.")
    else:
        st.error("Extraction incomplète.")
        st.session_state.pop("csv_dict", None)

# ---------- NOUVEAU : BOUTON DE TÉLÉCHARGEMENT RAPIDE ----------
if "csv_dict" in st.session_state and st.session_state["csv_dict"]:
    st.header("3. Télécharger les 4 CSV déjà chargés")
    if st.button("⬇️ Télécharger le ZIP sans ré-extraction"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in st.session_state["csv_dict"].items():
                zf.writestr(name, content.encode("utf-8-sig"))
        buf.seek(0)
        st.download_button(
            label=f"Télécharger {FICHIER_EXPORT_GLOBAL_ZIP}",
            data=buf.getvalue(),
            file_name=FICHIER_EXPORT_GLOBAL_ZIP,
            mime="application/zip",
        )
else:
    st.header("3. Télécharger les 4 CSV déjà chargés")
    st.info("Commencez par cliquer sur « Extraire et Télécharger Toutes les Données de Notion ».")
