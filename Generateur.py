import streamlit as st
import pandas as pd
import logging
import time
import httpx
import io
import json
from notion_client import Client
from notion_client.errors import RequestTimeoutError

# --- Configuration du logger ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Connexion à Notion et IDs des bases de données ---
try:
    NOTION_API_KEY = st.secrets["notion_api_key"]
    DATABASE_ID_INGREDIENTS = st.secrets["notion_database_id_ingredients"]

    notion = Client(auth=NOTION_API_KEY)
except KeyError as e:
    st.error(f"Erreur de configuration des secrets Notion : {e}. "
             "Veuillez vérifier votre fichier .streamlit/secrets.toml et vous assurer que 'notion_api_key' et 'notion_database_id_ingredients' sont définis.")
    st.stop() # Arrête l'exécution de l'application si les secrets ne sont pas configurés

# --- Fonctions d'extraction de propriétés Notion ---
def extract_property_value(prop):
    """Extrait la valeur d'une propriété de page Notion."""
    if not isinstance(prop, dict):
        return ""
    t = prop.get("type")
    if t == "title":
        return "".join([t.get("plain_text", "") for t in prop.get("title", [])])
    elif t == "rich_text":
        return "".join([t.get("plain_text", "") for t in prop.get("rich_text", [])])
    elif t == "multi_select":
        return ", ".join([opt.get("name", "") for opt in prop.get("multi_select", [])])
    elif t == "select":
        select_obj = prop.get("select")
        if select_obj is not None:
            return select_obj.get("name", "")
        return ""
    elif t == "number":
        return str(prop.get("number", ""))
    elif t == "checkbox":
        return str(prop.get("checkbox", ""))
    elif t == "date":
        date_obj = prop.get("date")
        if date_obj is not None:
            return date_obj.get("start", "")
        return ""
    elif t == "people":
        return ", ".join([person.get("name", "") for person in prop.get("people", [])])
    elif t == "relation":
        return ", ".join([rel.get("id", "") for rel in prop.get("relation", [])])
    elif t == "url":
        return prop.get("url", "")
    elif t == "email":
        return prop.get("email", "")
    elif t == "phone_number":
        return prop.get("phone_number", "")
    elif t == "formula":
        formula = prop.get("formula", {})
        if formula.get("type") == "string":
            return formula.get("string", "")
        elif formula.get("type") == "number":
            return str(formula.get("number", ""))
        elif formula.get("type") == "boolean":
            return str(formula.get("boolean", ""))
        elif formula.get("type") == "date":
            date_obj = formula.get("date")
            if date_obj is not None:
                return date_obj.get("start", "")
            return ""
    elif t == "rollup":
        rollup = prop.get("rollup", {})
        if rollup.get("type") == "array":
            return ", ".join([
                str(item.get("plain_text", "") or item.get("number", "") or "")
                for item in rollup.get("array", [])
            ])
        elif rollup.get("type") in ["number", "string", "boolean", "date"]:
            return str(rollup.get(rollup.get("type"), ""))
    return ""

# --- Fonctions de récupération des données Notion avec Caching Streamlit ---
@st.cache_data(show_spinner="Chargement des données Notion...", ttl=3600) # Cache pendant 1 heure
def fetch_notion_data(database_id: str, filter_json_str: str = None, columns_mapping: dict = None):
    """
    Récupère les données d'une base de données Notion et les retourne sous forme de DataFrame.
    Gère la pagination et les retries.
    filter_json_str: Filtre de la requête Notion sérialisé en JSON string (pour la compatibilité du cache).
    columns_mapping: Dictionnaire de mappage des noms de propriétés Notion vers les noms de colonnes DataFrame.
    """
    all_rows = []
    next_cursor = None
    total_extracted = 0
    batch_size = 100 # Taille de page max pour l'API Notion
    api_timeout_seconds = 60 # Timeout pour la requête API

    filter_cond = json.loads(filter_json_str) if filter_json_str else {}

    logger.info(f"Début de l'extraction de la base de données Notion: {database_id}")

    while True:
        try:
            query_params = {
                "database_id": database_id,
                "page_size": batch_size,
                "timeout": api_timeout_seconds,
            }
            if next_cursor:
                query_params["start_cursor"] = next_cursor
            if filter_cond: # Appliquer le filtre s'il existe
                query_params["filter"] = filter_cond

            results = notion.databases.query(**query_params)
            page_results = results.get("results", [])

            if not page_results:
                logger.info(f"Fin de l'extraction ou aucun résultat pour {database_id}.")
                break

            for result in page_results:
                properties = result.get("properties", {})
                row_data = {"Page_ID": result.get("id", "")}

                if columns_mapping:
                    for notion_prop, df_col in columns_mapping.items():
                        row_data[df_col] = extract_property_value(properties.get(notion_prop, {}))
                else:
                    # Fallback générique si aucun mapping n'est fourni.
                    for prop_name, prop_data in properties.items():
                        row_data[prop_name] = extract_property_value(prop_data)

                all_rows.append(row_data)
                total_extracted += 1

            next_cursor = results.get("next_cursor")
            if not next_cursor:
                break
            time.sleep(0.1) # Petit délai pour respecter les limites de débit de l'API

        except (httpx.TimeoutException, RequestTimeoutError) as e:
            logger.warning(f"Timeout détecté lors de la requête Notion ({database_id}): {e}. Réessai...")
            time.sleep(5) # Attendre plus longtemps en cas de timeout
            continue # Réessayer la même requête
        except Exception as e:
            logger.exception(f"Erreur inattendue lors de l'extraction Notion de {database_id}: {e}")
            st.error(f"Erreur lors de la récupération des données de Notion pour la base {database_id}: {e}")
            return pd.DataFrame() # Retourne un DataFrame vide en cas d'erreur grave

    if all_rows:
        df = pd.DataFrame(all_rows)
        logger.info(f"Extraction réussie : {total_extracted} lignes de {database_id}.")
        return df
    else:
        logger.info(f"Aucune donnée extraite de {database_id}.")
        return pd.DataFrame()

# Fonction spécifique pour la base de données Ingrédients
def get_ingredients_data():
    filter_cond = {"property": "Type de stock", "select": {"equals": "Autre type"}}
    columns_mapping = {
        "Nom": "Nom",
        "Type de stock": "Type de stock",
        "unité": "unité",
        "Qte reste": "Qte reste"
    }
    return fetch_notion_data(
        DATABASE_ID_INGREDIENTS,
        filter_json_str=json.dumps(filter_cond, sort_keys=True), # Convertir le dict en string hashable
        columns_mapping=columns_mapping
    )

# --- Fonction Principale de l'Application Streamlit ---
def main():
    st.set_page_config(layout="wide", page_title="Générateur de Menus Notion")
    st.title("🍽️ Générateur de Menus Automatisé avec Notion")

    st.sidebar.header("Chargement des Données")

    # 1. Chargement du fichier Planning.csv
    st.sidebar.subheader("1. Fichier Planning des Repas (.csv)")
    uploaded_planning_file = st.sidebar.file_uploader(
        "Choisissez votre fichier Planning.csv", type=["csv"], key="planning_uploader"
    )

    # Initialisation des DataFrames dans session_state si non présents
    if 'df_planning' not in st.session_state:
        st.session_state['df_planning'] = pd.DataFrame()
    if 'df_ingredients' not in st.session_state:
        st.session_state['df_ingredients'] = pd.DataFrame()

    if uploaded_planning_file is not None:
        try:
            df_planning_loaded = pd.read_csv(uploaded_planning_file, sep=None, engine='python')
            st.session_state['df_planning'] = df_planning_loaded
            st.sidebar.success("Fichier Planning.csv chargé avec succès.")
        except Exception as e:
            st.sidebar.error(f"Erreur lors du chargement de Planning.csv: {e}")
            st.session_state['df_planning'] = pd.DataFrame()
    else:
        st.sidebar.info("Veuillez charger votre fichier Planning.csv.")

    # 2. Récupération des données Notion (Ingrédients)
    st.sidebar.subheader("2. Données Ingrédients (Notion)")
    
    # Bouton de rechargement pour les données Ingrédients
    if st.sidebar.button("Charger/Recharger Ingrédients", key="reload_ingredients"):
        st.session_state['df_ingredients'] = get_ingredients_data()
        if not st.session_state['df_ingredients'].empty:
            st.sidebar.success(f"Données Ingrédients (Notion) chargées ({len(st.session_state['df_ingredients'])} lignes).")
        else:
            st.sidebar.warning("Aucune donnée Ingrédients chargée depuis Notion ou erreur.")
    # Charger au premier lancement si pas déjà en session_state
    elif st.session_state['df_ingredients'].empty:
        st.session_state['df_ingredients'] = get_ingredients_data()
        if not st.session_state['df_ingredients'].empty:
            st.sidebar.success(f"Données Ingrédients (Notion) chargées ({len(st.session_state['df_ingredients'])} lignes).")
        else:
            st.sidebar.warning("Aucune donnée Ingrédients chargée depuis Notion ou erreur.")
    else:
        st.sidebar.info("Données Ingrédients déjà chargées.")

    st.header("1. Vérification des Données Chargées")
    if not st.session_state['df_planning'].empty:
        st.write("✅ Planning.csv est chargé.")
        st.subheader("Aperçu de Planning.csv :")
        st.dataframe(st.session_state['df_planning'].head())
    else:
        st.write("❌ Planning.csv n'est pas encore chargé. Veuillez le charger dans la barre latérale.")

    if not st.session_state['df_ingredients'].empty:
        st.write("✅ Données Ingrédients (Notion) chargées.")
        st.subheader("Aperçu de la table Ingrédients (Notion) :")
        st.dataframe(st.session_state['df_ingredients'].head())
    else:
        st.write("❌ Données Ingrédients (Notion) manquantes ou non chargées.")
        st.info("Cliquez sur 'Charger/Recharger Ingrédients' dans la barre latérale pour les récupérer.")

    st.info("Pour le moment, l'application se limite au chargement du Planning.csv et des Ingrédients de Notion.")
    st.info("N'oubliez pas de configurer votre fichier `.streamlit/secrets.toml` avec les clés API et IDs de base de données nécessaires.")


if __name__ == "__main__":
    main()
