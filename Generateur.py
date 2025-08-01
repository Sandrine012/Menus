import streamlit as st
import pandas as pd
import logging
import time
import httpx
import io
import json
import zipfile # Import added for creating zip files
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
API_TIMEOUT_SECONDS = 180

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
    notion = Client(auth=NOTION_API_KEY, timeout_ms=API_TIMEOUT_SECONDS * 1000)
except KeyError as e:
    st.error(f"Erreur de configuration: La clé secrète Notion '{e}' est manquante. Veuillez la configurer dans Streamlit Cloud.")
    st.stop()
except Exception as e:
    st.error(f"Erreur lors de l'initialisation du client Notion: {e}")
    st.stop()

# --- Fonctions utilitaires d'extraction ---

def parse_property_value(property_data):
    """Analyse la valeur d'une propriété Notion en fonction de son type."""
    if not isinstance(property_data, dict):
        return ""

    prop_type = property_data.get('type')

    if prop_type == 'title':
        return "".join(t.get("plain_text", "") for t in property_data.get("title", []))
    elif prop_type == 'rich_text':
        return "".join(t.get("plain_text", "") for t in property_data.get("rich_text", []))
    elif prop_type == 'number':
        return property_data.get('number')
    elif prop_type == 'url':
        return property_data.get('url')
    elif prop_type == 'checkbox':
        return property_data.get('checkbox')
    elif prop_type == 'select':
        return property_data['select']['name'] if property_data.get('select') else ''
    elif prop_type == 'multi_select':
        return ', '.join([item['name'] for item in property_data.get('multi_select', [])])
    elif prop_type == 'date':
        if property_data.get('date') and property_data['date'].get('start'):
            start_date_str = property_data['date']['start']
            try:
                # Handle ISO format with or without timezone 'Z'
                dt_object = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
                return dt_object.strftime('%Y-%m-%d')
            except ValueError:
                return start_date_str # Return as is if parsing fails
        return ''
    elif prop_type == 'formula':
        formula_data = property_data.get('formula', {})
        formula_type = formula_data.get('type')
        if formula_type == 'number':
            return formula_data.get('number')
        elif formula_type == 'string':
            return formula_data.get('string')
        elif formula_type == 'boolean':
            return formula_data.get('boolean')
        elif formula_type == 'date':
            date_val = formula_data.get('date')
            if date_val and date_val.get('start'):
                try:
                    dt_object = datetime.fromisoformat(date_val['start'].replace('Z', '+00:00'))
                    return dt_object.strftime('%Y-%m-%d')
                except ValueError:
                    return date_val['start']
            return ''
        return None
    elif prop_type == 'relation':
        return ', '.join([item['id'] for item in property_data.get('relation', [])])
    elif prop_type == 'rollup':
        rollup_data = property_data.get('rollup', {})
        rollup_type = rollup_data.get('type')
        if rollup_type == 'array':
            values = []
            for item in rollup_data.get('array', []):
                if item.get('type') == 'rich_text':
                    values.append("".join(t.get("plain_text", "") for t in item.get("rich_text", [])))
                elif item.get('type') == 'title':
                    values.append("".join(t.get("text", {}).get("content", "") for t in item.get("title", [])))
                elif item.get('type') == 'number':
                    values.append(str(item.get('number')) if item.get('number') is not None else '')
                elif item.get('type') == 'formula': # Rollup of formula (e.g., Aime_pas_princip)
                    formula_val = parse_property_value({'type': 'formula', 'formula': item.get('formula')})
                    if formula_val is not None:
                        values.append(str(formula_val))
            return ', '.join(filter(None, values))
        elif rollup_type == 'number':
            return rollup_data.get('number')
        elif rollup_type == 'string':
            return rollup_data.get('string')
        # Add more rollup types if necessary
        return None
    elif prop_type == 'created_time':
        return datetime.fromisoformat(property_data['created_time'].replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
    elif prop_type == 'last_edited_time':
        return datetime.fromisoformat(property_data['last_edited_time'].replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
    elif prop_type == 'files':
        return ', '.join([file['name'] for file in property_data.get('files', [])])
    elif prop_type == 'email':
        return property_data.get('email')
    elif prop_type == 'phone_number':
        return property_data.get('phone_number')
    elif prop_type == 'people':
        return ', '.join([person['name'] if 'name' in person else person['id'] for person in property_data.get('people', [])])
    elif prop_type == 'status':
        return property_data['status']['name'] if property_data.get('status') else ''
    elif prop_type == 'unique_id':
        uid = property_data.get('unique_id', {})
        prefix = uid.get('prefix')
        number = uid.get('number')
        return f"{prefix}-{number}" if prefix and number is not None else (str(number) if number is not None else '')

    return None # Return None if the type is not handled

def query_notion_database(database_id, filter_obj=None, sort_obj=None, num_rows=NUM_ROWS_TO_EXTRACT):
    """
    Exécute une requête paginée sur une base de données Notion et retourne les résultats.
    """
    all_results = []
    start_cursor = None
    retries = 0

    while True:
        try:
            query_params = {
                "database_id": database_id,
                "page_size": BATCH_SIZE
            }
            if filter_obj:
                query_params["filter"] = filter_obj
            if sort_obj: # Only add if sort_obj is not None (i.e., it's an array)
                query_params["sorts"] = sort_obj
            if start_cursor:
                query_params["start_cursor"] = start_cursor

            response = notion.databases.query(**query_params)
            all_results.extend(response.get('results', []))
            if not response.get('has_more') or len(all_results) >= num_rows:
                break
            start_cursor = response.get('next_cursor')
            retries = 0 # Reset retries on successful call
        except (RequestTimeoutError, httpx.TimeoutException) as e:
            retries += 1
            if retries > MAX_RETRIES:
                logger.error(f"Tentatives maximales atteintes pour la base de données {database_id}. Abandon.")
                st.error(f"Échec de la connexion à Notion après plusieurs tentatives (timeout). Veuillez réessayer plus tard.")
                return None
            sleep_time = RETRY_DELAY_INITIAL * (2 ** (retries - 1))
            logger.warning(f"Timeout Notion pour {database_id}. Nouvelle tentative dans {sleep_time} secondes... ({retries}/{MAX_RETRIES})")
            time.sleep(sleep_time)
        except APIResponseError as e:
            logger.error(f"Erreur de l'API Notion pour la base de données {database_id}: {e.code} - {e.message}")
            st.error(f"Erreur de l'API Notion lors de l'extraction des données: {e.message}")
            return None
        except Exception as e:
            logger.error(f"Erreur inattendue lors de l'extraction de la base de données {database_id}: {e}", exc_info=True)
            st.error(f"Une erreur inattendue est survenue: {e}")
            return None
    return all_results

def extract_dataframe_from_notion(database_id, column_mapping, filename_for_log=""):
    """
    Extrait les données d'une base de données Notion et les convertit en DataFrame pandas
    selon un mappage de colonnes spécifié.
    """
    logger.info(f"Début de l'extraction pour {filename_for_log} depuis Notion...")
    data = []
    notion_pages = query_notion_database(database_id)

    if not notion_pages:
        logger.warning(f"Aucune donnée trouvée pour {filename_for_log} ou l'extraction a échoué.")
        return pd.DataFrame()

    for page in notion_pages:
        row = {'Page_ID': page['id']} # Ajout systématique de l'ID de la page Notion
        properties = page['properties']
        for csv_col, notion_prop_name in column_mapping.items():
            property_data = properties.get(notion_prop_name)
            row[csv_col] = parse_property_value(property_data)

        data.append(row)
    
    df = pd.DataFrame(data)
    logger.info(f"Extraction terminée pour {filename_for_log}. {len(df)} lignes extraites.")
    return df

@st.cache_data(show_spinner="Extraction des menus depuis Notion...", ttl=3600)
def get_menus_data():
    """Extrait et formate les données des menus depuis Notion."""
    column_mapping = {
        'Nom Menu': 'Nom', # Assurez-vous que 'Nom' est le nom exact de la propriété "title" dans Notion
        'Recette': 'Recette', # Ceci est une relation
        'Date': 'Date'
    }
    df_menus = extract_dataframe_from_notion(DATABASE_ID_MENUS, column_mapping, FICHIER_EXPORT_MENUS_CSV)

    # Convertir la colonne 'Date' au format YYYY-MM-DD
    if 'Date' in df_menus.columns and not df_menus['Date'].empty:
        df_menus['Date'] = pd.to_datetime(df_menus['Date'], errors='coerce').dt.strftime('%Y-%m-%d')

    # Réordonner les colonnes pour correspondre au CSV d'exemple
    if not df_menus.empty:
        df_menus = df_menus[['Nom Menu', 'Recette', 'Date']]
    return df_menus

@st.cache_data(show_spinner="Extraction des recettes depuis Notion...", ttl=3600)
def get_recipes_data():
    """Extrait et formate les données des recettes depuis Notion."""
    column_mapping = {
        'Nom': 'Nom_plat', # 'Nom_plat' est le nom de la colonne Title dans Notion
        'ID_Recette': 'ID_Recette', # Propriété Unique ID
        'Saison': 'Saison', # Multi-select
        'Calories': 'Calories Recette', # Rollup de nombre
        'Proteines': 'Proteines Recette', # Rollup de nombre
        'Temps_total': 'Temps_total', # Formule
        'Aime_pas_princip': 'Aime_pas_princip', # Rollup de formule (string)
        'Type_plat': 'Type_plat', # Multi-select
        'Transportable': 'Transportable' # Select ou Checkbox
    }
    df_recettes = extract_dataframe_from_notion(DATABASE_ID_RECETTES, column_mapping, FICHIER_EXPORT_RECETTES_CSV)

    # Réordonner les colonnes pour correspondre au CSV d'exemple (avec Page_ID en premier)
    if not df_recettes.empty:
        df_recettes = df_recettes[['Page_ID', 'Nom', 'ID_Recette', 'Saison', 'Calories', 'Proteines', 'Temps_total', 'Aime_pas_princip', 'Type_plat', 'Transportable']]
    return df_recettes

@st.cache_data(show_spinner="Extraction des ingrédients des recettes depuis Notion...", ttl=3600)
def get_ingredients_recettes_data():
    """Extrait et formate les données des ingrédients de recettes depuis Notion."""
    column_mapping = {
        'Qté/pers_s': 'Quantité/pers', # Nom de la propriété Notion pour la quantité (nombre)
        'Ingrédient ok': 'Ingrédient',  # Relation vers la DB Ingrédients
        'Type de stock f': 'Type de stock' # Nom de la propriété Notion (formule string)
    }
    df_ingredients_recettes = extract_dataframe_from_notion(DATABASE_ID_INGREDIENTS_RECETTES, column_mapping, FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV)

    # Convertir 'Qté/pers_s' en numérique
    if 'Qté/pers_s' in df_ingredients_recettes.columns:
        df_ingredients_recettes['Qté/pers_s'] = pd.to_numeric(
            df_ingredients_recettes['Qté/pers_s'].astype(str).str.replace(',', '.'),
            errors='coerce'
        )

    # Réordonner les colonnes pour correspondre au CSV d'exemple (avec Page_ID en premier)
    if not df_ingredients_recettes.empty:
        df_ingredients_recettes = df_ingredients_recettes[['Page_ID', 'Qté/pers_s', 'Ingrédient ok', 'Type de stock f']]
    return df_ingredients_recettes

@st.cache_data(show_spinner="Extraction des ingrédients depuis Notion...", ttl=3600)
def get_ingredients_data():
    """Extrait et formate les données des ingrédients depuis Notion."""
    column_mapping = {
        'Nom': 'Nom', # Nom de la propriété Title dans Notion
        'Type de stock': 'Type de stock', # Select
        'unité': 'Unité', # Select
        'Qte reste': 'Quantité restante' # Nombre
    }
    df_ingredients = extract_dataframe_from_notion(DATABASE_ID_INGREDIENTS, column_mapping, FICHIER_EXPORT_INGREDIENTS_CSV)

    # Convertir 'Qte reste' en numérique
    if 'Qte reste' in df_ingredients.columns:
        df_ingredients['Qte reste'] = pd.to_numeric(
            df_ingredients['Qte reste'].astype(str).str.replace(',', '.'),
            errors='coerce'
        )

    # Réordonner les colonnes pour correspondre au CSV d'exemple (avec Page_ID en premier)
    if not df_ingredients.empty:
        df_ingredients = df_ingredients[['Page_ID', 'Nom', 'Type de stock', 'unité', 'Qte reste']]
    return df_ingredients


# --- Application Streamlit ---
st.set_page_config(layout="centered", page_title="Générateur de Menus Notion")
st.title("🍽️ Générateur de Menus Automatisé avec Notion")

st.markdown("""
Cette application vous aide à gérer vos bases de données Notion pour les repas et les recettes,
et vous permet d'extraire vos données existantes.
""")

st.header("1. Vérification de la Configuration")
st.markdown("Assurez-vous que vos clés API et IDs de bases de données Notion sont correctement configurés dans les secrets Streamlit.")
st.info("""
    Pour configurer vos secrets Notion dans Streamlit Cloud:
    1. Allez dans votre espace de déploiement Streamlit.
    2. Cliquez sur `...` à côté de votre application, puis `Edit Secrets`.
    3. Ajoutez les clés suivantes avec leurs valeurs correspondantes:
        ```
        notion_api_key="votre_cle_api_notion"
        notion_database_id_ingredients="id_db_ingredients"
        notion_database_id_ingredients_recettes="id_db_ingredients_recettes"
        notion_database_id_recettes="id_db_recettes"
        notion_database_id_menus="id_db_menus"
        ```
    Assurez-vous que l'intégration Notion a bien accès à toutes les bases de données concernées.
    """)


st.header("2. Télécharger toutes les bases de données Notion (CSV)")
st.markdown("Cliquez sur le bouton ci-dessous pour extraire et télécharger l'ensemble de vos bases de données Notion (Menus, Recettes, Ingrédients_recettes, Ingrédients) au format CSV, regroupées dans un fichier ZIP.")

if st.button("Télécharger tous les fichiers CSV de Notion"):
    csv_data_dict = {}
    extraction_successful = True

    with st.spinner("Extraction des données de Notion en cours... Cela peut prendre un certain temps."):
        # Extraction des menus
        df_menus = get_menus_data()
        if df_menus is not None and not df_menus.empty:
            csv_data_dict[FICHIER_EXPORT_MENUS_CSV] = df_menus.to_csv(index=False, encoding="utf-8-sig")
            st.success(f"{FICHIER_EXPORT_MENUS_CSV} extrait ({len(df_menus)} lignes).")
        else:
            st.warning(f"Aucune donnée ou échec d'extraction pour {FICHIER_EXPORT_MENUS_CSV}.")
            extraction_successful = False

        # Extraction des recettes
        df_recettes = get_recipes_data()
        if df_recettes is not None and not df_recettes.empty:
            csv_data_dict[FICHIER_EXPORT_RECETTES_CSV] = df_recettes.to_csv(index=False, encoding="utf-8-sig")
            st.success(f"{FICHIER_EXPORT_RECETTES_CSV} extrait ({len(df_recettes)} lignes).")
        else:
            st.warning(f"Aucune donnée ou échec d'extraction pour {FICHIER_EXPORT_RECETTES_CSV}.")
            extraction_successful = False

        # Extraction des ingrédients_recettes
        df_ingredients_recettes = get_ingredients_recettes_data()
        if df_ingredients_recettes is not None and not df_ingredients_recettes.empty:
            csv_data_dict[FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV] = df_ingredients_recettes.to_csv(index=False, encoding="utf-8-sig")
            st.success(f"{FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV} extrait ({len(df_ingredients_recettes)} lignes).")
        else:
            st.warning(f"Aucune donnée ou échec d'extraction pour {FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV}.")
            extraction_successful = False

        # Extraction des ingrédients
        df_ingredients = get_ingredients_data()
        if df_ingredients is not None and not df_ingredients.empty:
            csv_data_dict[FICHIER_EXPORT_INGREDIENTS_CSV] = df_ingredients.to_csv(index=False, encoding="utf-8-sig")
            st.success(f"{FICHIER_EXPORT_INGREDIENTS_CSV} extrait ({len(df_ingredients)} lignes).")
        else:
            st.warning(f"Aucune donnée ou échec d'extraction pour {FICHIER_EXPORT_INGREDIENTS_CSV}.")
            extraction_successful = False

    if extraction_successful and csv_data_dict:
        # Créer un fichier ZIP en mémoire
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for filename, csv_content in csv_data_dict.items():
                zf.writestr(filename, csv_content.encode('utf-8-sig'))
        zip_buffer.seek(0) # Rembobiner le buffer au début

        st.download_button(
            label=f"Télécharger {FICHIER_EXPORT_GLOBAL_ZIP}",
            data=zip_buffer.getvalue(),
            file_name=FICHIER_EXPORT_GLOBAL_ZIP,
            mime="application/zip",
        )
        st.success("Tous les fichiers CSV sont prêts au téléchargement dans un fichier ZIP.")
    else:
        st.error("L'extraction des données depuis Notion a échoué pour un ou plusieurs fichiers, ou aucune donnée n'a été retournée.")

st.header("3. Génération de Nouveaux Menus (Fonctionnalité à venir)")
st.markdown("Cette section contiendra les outils pour générer de nouveaux menus basés sur vos critères et les données de vos bases Notion.")
st.warning("Cette fonctionnalité n'est pas encore implémentée dans cette version du code.")

st.info("N'oubliez pas de configurer vos secrets Notion dans Streamlit Cloud.")

if __name__ == '__main__':
    # Le code principal de l'application Streamlit est directement dans le script.
    pass
