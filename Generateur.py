import streamlit as st
import pandas as pd
import logging
import time
import httpx
import io
import json
import zipfile
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError
from datetime import datetime

# --- Configuration du logger ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Constantes pour l'extraction de recettes et menus ---
SAISON_FILTRE = "Printemps" # Peut être rendu configurable via un widget Streamlit si désiré
NUM_ROWS_TO_EXTRACT = 100000 # Augmenté pour l'extraction des menus
BATCH_SIZE = 50
MAX_RETRIES = 7
RETRY_DELAY_INITIAL = 10
API_TIMEOUT_SECONDS = 180 # Cette constante n'est plus directement utilisée pour le client Notion ici

# --- Noms de fichiers pour l'export CSV ---
FICHIER_EXPORT_MENUS_CSV = "Menus.csv"
FICHIER_EXPORT_RECETTES_CSV = "Recettes.csv"
FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV = "Ingredients_recettes.csv"
FICHIER_EXPORT_INGREDIENTS_CSV = "Ingredients.csv"
FICHIER_EXPORT_GLOBAL_ZIP = "Notion_Exports.zip"

# --- Connexion à Notion et IDs des bases de données ---
try:
    NOTION_API_KEY = st.secrets["notion_api_key"]
    DATABASE_ID_INGREDIENTS = st.secrets["notion_database_id_ingredients"]
    DATABASE_ID_INGREDIENTS_RECETTES = st.secrets["notion_database_id_ingredients_recettes"]
    DATABASE_ID_RECETTES = st.secrets["notion_database_id_recettes"]
    DATABASE_ID_MENUS = st.secrets["notion_database_id_menus"]
    
    notion = Client(auth=NOTION_API_KEY)
    
    logger.info("Client Notion initialisé.")
except Exception as e:
    st.error(f"Erreur de configuration ou de connexion à Notion : {e}")
    st.info("Veuillez vous assurer que les secrets Notion sont correctement configurés dans Streamlit Cloud ou dans le fichier secrets.toml.")
    st.stop()

# ========== FONCTION D'EXTRACTION DE PROPRIÉTÉS (Basée sur votre code Colab) ==========
def get_property_value(prop_data, notion_prop_name_for_log, expected_format_key):
    if not prop_data: return ""
    prop_type = prop_data.get("type")

    try:
        if expected_format_key == "title":
            return "".join(t.get("text", {}).get("content", "") for t in prop_data.get("title", []))
        elif expected_format_key == "rollup_text_concat":
            if prop_type == "rollup":
                arr = prop_data.get("rollup", {}).get("array", [])
                values = []
                for item in arr:
                    if item.get("type") == "rich_text": values.append("".join(t.get("plain_text", "") for t in item.get("rich_text", [])))
                    elif item.get("type") == "title": values.append("".join(t.get("text", {}).get("content", "") for t in item.get("title", [])))
                return ", ".join(filter(None, values))
            return ""
        elif expected_format_key == "unique_id_or_text":
            if prop_type == "unique_id":
                uid = prop_data.get("unique_id", {}); p, n = uid.get("prefix"), uid.get("number")
                return f"{p}-{n}" if p and n is not None else (str(n) if n is not None else "")
            elif prop_type == "title": return get_property_value(prop_data, notion_prop_name_for_log, "title")
            elif expected_format_key == "rich_text_plain": return get_property_value(prop_data, notion_prop_name_for_log, "rich_text_plain")
            return ""
        elif expected_format_key == "rich_text_plain":
            return "".join(t.get("plain_text", "") for t in prop_data.get("rich_text", []))
        elif expected_format_key == "multi_select_comma_separated":
            if prop_type == "multi_select":
                return ", ".join(filter(None, [o.get("name", "") for o in prop_data.get("multi_select", [])]))
            return ""
        elif expected_format_key == "number_to_string_or_empty":
            if prop_type == "number": num = prop_data.get("number"); return str(num) if num is not None else ""
            return ""
        elif expected_format_key == "formula_number_or_string_or_empty":
            if prop_type == "formula":
                fo = prop_data.get("formula", {}); ft = fo.get("type")
                if ft == "number": num = fo.get("number"); return str(num) if num is not None else ""
                elif ft == "string": return fo.get("string", "")
            return ""
        elif expected_format_key == "rollup_single_number_or_empty":
            if prop_type == "rollup":
                ro = prop_data.get("rollup", {}); rt = ro.get("type")
                if rt == "number": num = ro.get("number"); return str(num) if num is not None else ""
                elif rt == "array":
                    arr = ro.get("array", [])
                    if arr:
                        item = arr[0]; it = item.get("type")
                        if it == "number": num = item.get("number"); return str(num) if num is not None else ""
                        elif it == "formula":
                            fi = item.get("formula", {}); fit = fi.get("type")
                            if fit == "number": num = fi.get("number"); return str(num) if num is not None else ""
                return ""
            return ""
        elif expected_format_key == "rollup_formula_string_dots_comma_separated":
            if prop_type == "rollup":
                arr = prop_data.get("rollup", {}).get("array", [])
                vals = []
                for item in arr:
                    if item.get("type") == "formula":
                        fo = item.get("formula", {}); ft = fo.get("type")
                        if ft == "string": sv = fo.get("string"); vals.append(sv if sv and sv.strip() else ".")
                    else: vals.append(".")
                return ", ".join(vals)
            return ""
        elif expected_format_key == "select_to_oui_empty":
            if prop_type == "select":
                so = prop_data.get("select"); return "Oui" if so and so.get("name", "").lower() == "oui" else ""
            elif prop_type == "checkbox": return "Oui" if prop_data.get("checkbox", False) else ""
            return ""
        elif expected_format_key == "relation_id_or_empty":
             if prop_type == "relation":
                 relation_ids = [r["id"] for r in prop_data.get("relation", []) if r.get("id")]
                 return relation_ids[0] if relation_ids else "" # Prend le premier ID si multiple
             return ""
        elif expected_format_key == "date_start_or_empty":
            if prop_type == "date":
                date_obj = prop_data.get("date")
                if date_obj and date_obj.get("start"):
                    return date_obj["start"]
            return ""
    except Exception as e:
        logger.error(f"EXC Formatage: '{notion_prop_name_for_log}' (format: {expected_format_key}): {e}", exc_info=False)
        return "ERREUR_FORMAT"
    return ""


# --- Fonctions d'extraction de données ---
def fetch_data_from_notion(database_id, num_rows, filter_conditions=None):
    results = []
    has_more = True
    start_cursor = None
    retries = 0

    query_payload = {
        "page_size": BATCH_SIZE
    }
    if filter_conditions:
        query_payload["filter"] = filter_conditions

    while has_more and len(results) < num_rows and retries < MAX_RETRIES:
        try:
            if start_cursor:
                query_payload["start_cursor"] = start_cursor
            
            response = notion.databases.query(
                database_id=database_id,
                **query_payload
            )
            
            results.extend(response["results"])
            has_more = response["has_more"]
            start_cursor = response["next_cursor"]
            retries = 0 # Reset retries on successful call

            if has_more and len(results) < num_rows:
                time.sleep(RETRY_DELAY_INITIAL) # Respect Notion API rate limits
                
        except (RequestTimeoutError, httpx.TimeoutException, httpx.ReadTimeout):
            logger.warning(f"Timeout occurred, retrying in {RETRY_DELAY_INITIAL * (retries + 1)} seconds...")
            time.sleep(RETRY_DELAY_INITIAL * (retries + 1))
            retries += 1
        except APIResponseError as e:
            logger.error(f"Notion API error: {e}")
            retries += 1
            time.sleep(RETRY_DELAY_INITIAL * (retries + 1))
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}")
            retries += 1
            time.sleep(RETRY_DELAY_INITIAL * (retries + 1))

    if retries >= MAX_RETRIES:
        logger.error(f"Failed to fetch data from {database_id} after {MAX_RETRIES} retries.")
        return []
        
    return results[:num_rows]


# --- Mappings pour chaque type de CSV ---
# (Basé sur le mapping de votre script Colab et adapté pour les autres bases de données)

# Pour Recettes.csv
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
    "Transportable": ("Transportable", "select_to_oui_empty")
}
header_recipes = list(mapping_recipes.keys())

# Pour Menus.csv (Page_ID retiré pour la sortie CSV)
mapping_menus = {
    "Nom Menu": ("Nom Menu", "title"),
    "Recette": ("Recette", "relation_id_or_empty"), # Récupère l'ID de la recette liée
    "Date": ("Date", "date_start_or_empty")
}
header_menus = list(mapping_menus.keys())

# Pour Ingredients.csv
mapping_ingredients = {
    "Page_ID": (None, "page_id_special"),
    "Nom": ("Nom", "title"),
    "Type de stock": ("Type de stock", "select_to_oui_empty"), # Assumons "Type de stock" est un select
    "unité": ("unité", "rich_text_plain"),
    "Qte reste": ("Qté reste", "number_to_string_or_empty")
}
header_ingredients = list(mapping_ingredients.keys())

# Pour Ingredients_recettes.csv
mapping_ingredients_recettes = {
    "Page_ID": (None, "page_id_special"),
    "Qté/pers_s": ("Qté/pers_s", "number_to_string_or_empty"),
    "Ingrédient ok": ("Ingrédient ok", "relation_id_or_empty"), # Récupère l'ID de l'ingrédient lié
    "Type de stock f": ("Type de stock f", "select_to_oui_empty") # Assumons "Type de stock f" est un select
}
header_ingredients_recettes = list(mapping_ingredients_recettes.keys())


def process_notion_pages_to_dataframe(pages, mapping, default_header):
    data_list = []
    for page in pages:
        row_data = {}
        page_props_raw = page.get("properties", {})
        for csv_col_name, (notion_prop_name_key, expected_format_key) in mapping.items():
            if csv_col_name == "Page_ID": 
                row_data[csv_col_name] = page.get("id", "")
            else:
                raw_prop_data = page_props_raw.get(notion_prop_name_key)
                value = get_property_value(raw_prop_data, notion_prop_name_key, expected_format_key)
                row_data[csv_col_name] = value
        data_list.append(row_data)
    
    # Créer un DataFrame avec l'ordre des colonnes par défaut
    df = pd.DataFrame(data_list)
    
    # S'assurer que les colonnes sont dans le bon ordre défini par le header
    # Et ajouter les colonnes manquantes si besoin (remplies par NaN puis converties en chaîne vide)
    existing_cols = df.columns.tolist()
    final_cols = []
    for col in default_header:
        if col in existing_cols:
            final_cols.append(col)
        else:
            df[col] = "" # Ajouter la colonne manquante comme vide
            final_cols.append(col)
            logger.warning(f"La colonne '{col}' n'était pas présente dans les données extraites et a été ajoutée vide.")

    df = df[final_cols] # Ensure column order
    
    # --- Conversion de la colonne 'Date' pour données extraites de Notion ---
    if 'Date' in df.columns:
        df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
        # Formater la date en YYYY-MM-DD string après conversion pour l'uniformité
        df['Date'] = df['Date'].dt.strftime('%Y-%m-%d')
    # --- FIN AJOUT ---

    return df


def get_notion_recipes_data():
    logger.info("Début de l'extraction des recettes depuis Notion.")
    # Le filtre doit être défini ici selon la logique de votre Colab
    filter_conditions_recipes = [
        {"property": "Elément parent", "relation": {"is_empty": True}},
        {
            "or": [
                {"property": "Saison", "multi_select": {"contains": "Toute l'année"}},
                *([{"property": "Saison", "multi_select": {"contains": SAISON_FILTRE}}] if SAISON_FILTRE else []),
                {"property": "Saison", "multi_select": {"is_empty": True}}
            ]
        }
    ]
    # Ajout du filtre sur Type_plat
    filter_conditions_recipes.append(
        {
            "or": [
                {"property": "Type_plat", "multi_select": {"contains": "Salade"}},
                {"property": "Type_plat", "multi_select": {"contains": "Soupe"}},
                {"property": "Type_plat", "multi_select": {"contains": "Plat"}}
            ]
        }
    )
    filter_recettes_api = {"and": filter_conditions_recipes}

    recipes_pages = fetch_data_from_notion(DATABASE_ID_RECETTES, NUM_ROWS_TO_EXTRACT, filter_recettes_api)
    df_recipes = process_notion_pages_to_dataframe(recipes_pages, mapping_recipes, header_recipes)
    
    logger.info(f"Extraction de {len(df_recipes)} recettes terminée.")
    return df_recipes

def get_notion_ingredients_data():
    logger.info("Début de l'extraction des ingrédients depuis Notion.")
    ingredients_pages = fetch_data_from_notion(DATABASE_ID_INGREDIENTS, NUM_ROWS_TO_EXTRACT)
    df_ingredients = process_notion_pages_to_dataframe(ingredients_pages, mapping_ingredients, header_ingredients)
    logger.info(f"Extraction de {len(df_ingredients)} ingrédients terminée.")
    return df_ingredients

def get_notion_ingredients_recipes_data():
    logger.info("Début de l'extraction des liens ingrédients-recettes depuis Notion.")
    ing_rec_pages = fetch_data_from_notion(DATABASE_ID_INGREDIENTS_RECETTES, NUM_ROWS_TO_EXTRACT)
    df_ing_rec = process_notion_pages_to_dataframe(ing_rec_pages, mapping_ingredients_recettes, header_ingredients_recettes)
    logger.info(f"Extraction de {len(df_ing_rec)} liens ingrédients-recettes terminée.")
    return df_ing_rec

def get_existing_menus_data():
    logger.info("Début de l'extraction des menus existants depuis Notion.")
    menus_pages = fetch_data_from_notion(DATABASE_ID_MENUS, NUM_ROWS_TO_EXTRACT)
    df_menus = process_notion_pages_to_dataframe(menus_pages, mapping_menus, header_menus)
    logger.info(f"Extraction de {len(df_menus)} menus existants terminée.")
    return df_menus

# --- Initialisation de session_state pour stocker les DataFrames ---
if 'df_recipes_extracted' not in st.session_state:
    st.session_state.df_recipes_extracted = pd.DataFrame()
if 'df_ingredients_extracted' not in st.session_state:
    st.session_state.df_ingredients_extracted = pd.DataFrame()
if 'df_ing_rec_extracted' not in st.session_state:
    st.session_state.df_ing_rec_extracted = pd.DataFrame()


# --- Section Streamlit ---
st.set_page_config(layout="wide")
st.title("Application de Génération de Menus Notion")

st.header("1. Vérification de la Connexion et Configuration")
st.success("Connexion à Notion réussie et variables d'environnement chargées.")
st.info("Assurez-vous que les bases de données Notion sont accessibles et contiennent les propriétés nécessaires.")


st.header("2. Extraction des Données de Notion vers CSV/ZIP")
st.markdown("Cochez les types de données que vous souhaitez extraire de Notion et télécharger :")

with st.form("extraction_form"):
    col1, col2 = st.columns(2)
    with col1:
        extract_recipes = st.checkbox("Recettes", value=True)
        extract_ingredients = st.checkbox("Ingrédients", value=True)
    with col2:
        extract_ing_rec = st.checkbox("Ingrédients_recettes", value=True)
        extract_menus = st.checkbox("Menus", value=True)
    
    submitted = st.form_submit_button("Extraire et Télécharger les données sélectionnées")

    if submitted:
        csv_data_dict = {}
        extraction_successful = True
        selected_count = 0

        if extract_recipes:
            selected_count += 1
            with st.spinner("Extraction des recettes depuis Notion..."):
                st.session_state.df_recipes_extracted = get_notion_recipes_data()
                if not st.session_state.df_recipes_extracted.empty:
                    csv_data_dict[FICHIER_EXPORT_RECETTES_CSV] = st.session_state.df_recipes_extracted.to_csv(index=False, encoding="utf-8-sig")
                    st.success(f"Recettes extraites : {len(st.session_state.df_recipes_extracted)} lignes.")
                else:
                    st.error(f"L'extraction des recettes a échoué ou n'a retourné aucune donnée pour {FICHIER_EXPORT_RECETTES_CSV}.")
                    extraction_successful = False

        if extract_ingredients:
            selected_count += 1
            with st.spinner("Extraction des ingrédients depuis Notion..."):
                st.session_state.df_ingredients_extracted = get_notion_ingredients_data()
                if not st.session_state.df_ingredients_extracted.empty:
                    csv_data_dict[FICHIER_EXPORT_INGREDIENTS_CSV] = st.session_state.df_ingredients_extracted.to_csv(index=False, encoding="utf-8-sig")
                    st.success(f"Ingrédients extraits : {len(st.session_state.df_ingredients_extracted)} lignes.")
                else:
                    st.error(f"L'extraction des ingrédients a échoué ou n'a retourné aucune donnée pour {FICHIER_EXPORT_INGREDIENTS_CSV}.")
                    extraction_successful = False

        if extract_ing_rec:
            selected_count += 1
            with st.spinner("Extraction des liens ingrédients-recettes depuis Notion..."):
                st.session_state.df_ing_rec_extracted = get_notion_ingredients_recipes_data()
                if not st.session_state.df_ing_rec_extracted.empty:
                    csv_data_dict[FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV] = st.session_state.df_ing_rec_extracted.to_csv(index=False, encoding="utf-8-sig")
                    st.success(f"Liens ingrédients-recettes extraits : {len(st.session_state.df_ing_rec_extracted)} lignes.")
                else:
                    st.error(f"L'extraction des liens ingrédients-recettes a échoué ou n'a retourné aucune donnée pour {FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV}.")
                    extraction_successful = False
            
        if extract_menus:
            selected_count += 1
            with st.spinner("Extraction des menus existants depuis Notion..."):
                df_menus_from_notion = get_existing_menus_data() # This already returns a formatted DF
                if df_menus_from_notion is not None and not df_menus_from_notion.empty:
                    csv_data_dict[FICHIER_EXPORT_MENUS_CSV] = df_menus_from_notion.to_csv(index=False, encoding="utf-8-sig")
                    st.success(f"Menus existants extraits : {len(df_menus_from_notion)} lignes.")
                else:
                    st.error(f"L'extraction des menus existants depuis Notion a échoué ou n'a retourné aucune donnée pour {FICHIER_EXPORT_MENUS_CSV}.")
                    extraction_successful = False

        if not selected_count:
            st.warning("Veuillez sélectionner au moins un type de données à extraire.")
            extraction_successful = False # Ensure no download button appears if nothing selected

        if extraction_successful and csv_data_dict:
            if len(csv_data_dict) == 1:
                # If only one file, offer direct download
                filename, csv_content = list(csv_data_dict.items())[0]
                st.download_button(
                    label=f"Télécharger {filename}",
                    data=csv_content.encode('utf-8-sig'),
                    file_name=filename,
                    mime="text/csv",
                    key=f"download_{filename}"
                )
                st.success(f"Fichier '{filename}' prêt au téléchargement.")
            elif len(csv_data_dict) > 1:
                # If multiple files, offer ZIP download
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for filename, csv_content in csv_data_dict.items():
                        zf.writestr(filename, csv_content.encode('utf-8-sig'))
                zip_buffer.seek(0)

                st.download_button(
                    label=f"Télécharger {FICHIER_EXPORT_GLOBAL_ZIP}",
                    data=zip_buffer.getvalue(),
                    file_name=FICHIER_EXPORT_GLOBAL_ZIP,
                    mime="application/zip",
                    key="download_all_csvs_zip"
                )
                st.success("Tous les fichiers CSV sélectionnés sont prêts au téléchargement dans un fichier ZIP.")
        elif not extraction_successful:
            st.error("L'extraction des données depuis Notion a échoué pour un ou plusieurs fichiers, ou aucune donnée n'a été retournée.")


st.header("3. Génération de Nouveaux Menus (Fonctionnalité à venir)")
st.markdown("Cette section contiendra les outils pour générer de nouveaux menus basés sur vos critères et les données de vos bases Notion.")
st.warning("Cette fonctionnalité n'est pas encore implémentée dans cette version du code.")

st.info("N'oubliez pas de configurer vos secrets Notion dans Streamlit Cloud.")


if __name__ == "__main__":
    pass
