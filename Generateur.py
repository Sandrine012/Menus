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

def query_notion_database(database_id, filter_obj=None, sort_obj=None, num_rows=NUM_ROWS_TO_EXTRACT):
    """
    Exécute une requête paginée sur une base de données Notion et retourne les résultats.
    """
    all_results = []
    start_cursor = None
    retries = 0

    while True:
        try:
            # st.write(f"Requête sur la base de données {database_id} avec cursor {start_cursor}") # Pour le débogage
            response = notion.databases.query(
                database_id=database_id,
                filter=filter_obj,
                sorts=sort_obj,
                start_cursor=start_cursor,
                page_size=BATCH_SIZE
            )
            all_results.extend(response.get('results', []))
            if not response.get('has_more'):
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
            logger.error(f"Erreur de l'API Notion pour la base de données {database_id}: {e}")
            st.error(f"Erreur de l'API Notion lors de l'extraction des données: {e}")
            return None
        except Exception as e:
            logger.error(f"Erreur inattendue lors de l'extraction de la base de données {database_id}: {e}")
            st.error(f"Une erreur inattendue est survenue: {e}")
            return None
    return all_results

def parse_property_value(property_data):
    """Analyse la valeur d'une propriété Notion en fonction de son type."""
    prop_type = property_data.get('type')
    if prop_type == 'title':
        return property_data['title'][0]['plain_text'] if property_data['title'] else ''
    elif prop_type == 'rich_text':
        return property_data['rich_text'][0]['plain_text'] if property_data['rich_text'] else ''
    elif prop_type == 'number':
        return property_data['number']
    elif prop_type == 'url':
        return property_data['url']
    elif prop_type == 'checkbox':
        return property_data['checkbox']
    elif prop_type == 'select':
        return property_data['select']['name'] if property_data['select'] else ''
    elif prop_type == 'multi_select':
        return ', '.join([item['name'] for item in property_data['multi_select']])
    elif prop_type == 'date':
        if property_data['date']:
            start = property_data['date'].get('start')
            return datetime.fromisoformat(start).strftime('%Y-%m-%d') if start else ''
        return ''
    elif prop_type == 'formula':
        # Les formules peuvent être de différents types, nous essayons de récupérer la valeur
        formula_type = property_data['formula'].get('type')
        if formula_type == 'number':
            return property_data['formula'].get('number')
        elif formula_type == 'string':
            return property_data['formula'].get('string')
        elif formula_type == 'boolean':
            return property_data['formula'].get('boolean')
        elif formula_type == 'date':
            date_val = property_data['formula'].get('date')
            if date_val and date_val.get('start'):
                return datetime.fromisoformat(date_val['start']).strftime('%Y-%m-%d')
            return ''
        return ''
    elif prop_type == 'relation':
        # Pour les relations, nous retournons simplement les IDs pour l'instant
        return ', '.join([item['id'] for item in property_data['relation']])
    elif prop_type == 'rollup':
        # Les rollups peuvent être complexes, simplifions pour l'export CSV
        rollup_type = property_data['rollup'].get('type')
        if rollup_type == 'array': # Par exemple, si c'est un rollup de multi_selects
            # Tente de gérer les cas où les rollups sont des tableaux d'objets avec 'name'
            if property_data['rollup']['array'] and isinstance(property_data['rollup']['array'][0], dict) and 'name' in property_data['rollup']['array'][0]:
                return ', '.join([item['name'] for item in property_data['rollup']['array']])
            # Si c'est un rollup de nombres
            elif property_data['rollup']['array'] and isinstance(property_data['rollup']['array'][0], dict) and 'number' in property_data['rollup']['array'][0]:
                return ', '.join([str(item['number']) for item in property_data['rollup']['array'] if item['number'] is not None])
            # Si c'est un rollup de rich_text (ex: nom d'ingrédient)
            elif property_data['rollup']['array'] and isinstance(property_data['rollup']['array'][0], dict) and 'rich_text' in property_data['rollup']['array'][0]:
                text_values = []
                for item in property_data['rollup']['array']:
                    if item['rich_text']:
                        text_values.append(item['rich_text'][0]['plain_text'])
                return ', '.join(text_values)
            return str(property_data['rollup']['array']) # Fallback pour autres types de tableau
        elif rollup_type == 'number':
            return property_data['rollup'].get('number')
        elif rollup_type == 'date':
            date_val = property_data['rollup'].get('date')
            if date_val and date_val.get('start'):
                return datetime.fromisoformat(date_val['start']).strftime('%Y-%m-%d')
            return ''
        elif rollup_type == 'formula': # Rollup de formule
            formula_data = property_data['rollup']['formula']
            return parse_property_value({'type': formula_data.get('type'), formula_data.get('type'): formula_data.get(formula_data.get('type'))})
        elif rollup_type == 'string':
            return property_data['rollup'].get('string')
        return None # Ou une valeur par défaut appropriée
    elif prop_type == 'created_time':
        return datetime.fromisoformat(property_data['created_time'].replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
    elif prop_type == 'last_edited_time':
        return datetime.fromisoformat(property_data['last_edited_time'].replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
    elif prop_type == 'files':
        return ', '.join([file['name'] for file in property_data['files']]) if property_data['files'] else ''
    elif prop_type == 'email':
        return property_data['email']
    elif prop_type == 'phone_number':
        return property_data['phone_number']
    elif prop_type == 'people':
        return ', '.join([person['name'] if 'name' in person else person['id'] for person in property_data['people']])
    elif prop_type == 'status':
        return property_data['status']['name'] if property_data['status'] else ''
    # Ajouter d'autres types de propriétés Notion si nécessaire
    return None # Retourne None si le type n'est pas géré

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
            if property_data:
                row[csv_col] = parse_property_value(property_data)
            else:
                row[csv_col] = None # Ou une chaîne vide, selon la préférence

        data.append(row)
    
    df = pd.DataFrame(data)
    logger.info(f"Extraction terminée pour {filename_for_log}. {len(df)} lignes extraites.")
    return df

def get_menus_data():
    """Extrait et formate les données des menus depuis Notion."""
    column_mapping = {
        'Nom Menu': 'Nom',
        'Recette': 'Recette', # Ceci est une relation, nous aurons besoin des IDs
        'Date': 'Date'
    }
    df_menus = extract_dataframe_from_notion(DATABASE_ID_MENUS, column_mapping, FICHIER_EXPORT_MENUS_CSV)

    if not df_menus.empty:
        # Pour la colonne 'Recette', qui est une relation, le `parse_property_value` retourne les IDs.
        # Si vous voulez les noms des recettes, il faudrait faire une jointure avec la table Recettes.
        # Pour l'instant, on laisse les IDs comme dans votre exemple 'Menus.csv'.
        pass # Pas de traitement spécifique nécessaire si les IDs sont suffisants.

    # Réordonner les colonnes pour correspondre au CSV d'exemple
    if not df_menus.empty:
        df_menus = df_menus[['Nom Menu', 'Recette', 'Date']]
    return df_menus

def get_recipes_data():
    """Extrait et formate les données des recettes depuis Notion."""
    column_mapping = {
        'Nom': 'Nom',
        'ID_Recette': 'ID_Recette',
        'Saison': 'Saison',
        'Calories': 'Calories',
        'Proteines': 'Protéines',
        'Temps_total': 'Temps total (min)',
        'Aime_pas_princip': 'Aime pas princip',
        'Type_plat': 'Type de plat',
        'Transportable': 'Transportable'
    }
    df_recettes = extract_dataframe_from_notion(DATABASE_ID_RECETTES, column_mapping, FICHIER_EXPORT_RECETTES_CSV)

    # La colonne 'Aime_pas_princip' est un multi-select, elle est déjà gérée par parse_property_value pour retourner une chaîne.
    # La colonne 'Type_plat' est un multi-select, elle est déjà gérée par parse_property_value pour retourner une chaîne.
    # 'Transportable' est une checkbox, gérée.

    # Réordonner les colonnes pour correspondre au CSV d'exemple (avec Page_ID en premier)
    if not df_recettes.empty:
        df_recettes = df_recettes[['Page_ID', 'Nom', 'ID_Recette', 'Saison', 'Calories', 'Proteines', 'Temps_total', 'Aime_pas_princip', 'Type_plat', 'Transportable']]
    return df_recettes

def get_ingredients_recettes_data():
    """Extrait et formate les données des ingrédients de recettes depuis Notion."""
    column_mapping = {
        'Qté/pers_s': 'Quantité/pers', # Nom de la propriété Notion
        'Ingrédient ok': 'Ingrédient',  # Relation vers la DB Ingrédients
        'Type de stock f': 'Type de stock' # Nom de la propriété Notion
    }
    df_ingredients_recettes = extract_dataframe_from_notion(DATABASE_ID_INGREDIENTS_RECETTES, column_mapping, FICHIER_EXPORT_INGREDIENTS_RECETTES_CSV)

    # 'Ingrédient ok' est une relation, `parse_property_value` retourne l'ID
    # Réordonner les colonnes pour correspondre au CSV d'exemple (avec Page_ID en premier)
    if not df_ingredients_recettes.empty:
        df_ingredients_recettes = df_ingredients_recettes[['Page_ID', 'Qté/pers_s', 'Ingrédient ok', 'Type de stock f']]
    return df_ingredients_recettes

def get_ingredients_data():
    """Extrait et formate les données des ingrédients depuis Notion."""
    column_mapping = {
        'Nom': 'Nom',
        'Type de stock': 'Type de stock',
        'unité': 'Unité',
        'Qte reste': 'Quantité restante'
    }
    df_ingredients = extract_dataframe_from_notion(DATABASE_ID_INGREDIENTS, column_mapping, FICHIER_EXPORT_INGREDIENTS_CSV)

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
