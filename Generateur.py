import streamlit as st
import pandas as pd
import logging
import time
import httpx
import io
import json
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError

# --- Configuration du logger ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Connexion à Notion et IDs des bases de données ---
try:
    NOTION_API_KEY = st.secrets["notion_api_key"]
    DATABASE_ID_INGREDIENTS = st.secrets["notion_database_id_ingredients"]
    DATABASE_ID_INGREDIENTS_RECETTES = st.secrets["notion_database_id_ingredients_recettes"]
    DATABASE_ID_RECETTES = st.secrets["notion_database_id_recettes"] # Nouvelle DB pour Recettes

    notion = Client(auth=NOTION_API_KEY)
except KeyError as e:
    st.error(f"Erreur de configuration des secrets Notion : {e}. "
             "Veuillez vérifier votre fichier .streamlit/secrets.toml et vous assurer que 'notion_api_key', "
             "'notion_database_id_ingredients', 'notion_database_id_ingredients_recettes' et 'notion_database_id_recettes' sont définis.")
    st.stop() # Arrête l'exécution de l'application si les secrets ne sont pas configurés

# --- Constantes pour la nouvelle extraction de recettes ---
SAISON_FILTRE = "Printemps" # Peut être rendu configurable via un widget Streamlit si désiré
NUM_ROWS_TO_EXTRACT = 400
BATCH_SIZE = 50
MAX_RETRIES = 3
RETRY_DELAY_INITIAL = 5

# ========== NOUVELLE FONCTION D'EXTRACTION DE PROPRIÉTÉS SPÉCIFIQUE POUR LES RECETTES ==========
# Cette fonction est basée sur le 'get_property_value' fourni par l'utilisateur
def extract_recette_property_value(prop_data, notion_prop_name_for_log, expected_format_key):
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
            elif prop_type == "title": return extract_recette_property_value(prop_data, notion_prop_name_for_log, "title")
            elif prop_type == "rich_text": return extract_recette_property_value(prop_data, notion_prop_name_for_log, "rich_text_plain")
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
        elif expected_format_key == "rollup_formula_string_dots_comma_separated":
            if prop_type == "rollup":
                arr = prop_data.get("rollup", {}).get("array", [])
                vals = []
                for item in arr:
                    if item.get("type") == "formula":
                        fo = item.get("formula", {}); ft = fo.get("type")
                        if ft == "string": sv = fo.get("string"); vals.append(sv if sv and sv.strip() else ".")
                        else: vals.append(".")
                    else: vals.append(".")
                return ", ".join(vals)
            return ""
        elif expected_format_key == "select_to_oui_empty":
            if prop_type == "select":
                so = prop_data.get("select"); return "Oui" if so and so.get("name", "").lower() == "oui" else ""
            elif prop_type == "checkbox": return "Oui" if prop_data.get("checkbox", False) else ""
            return ""
    except Exception as e:
        logger.error(f"EXC Formatage: '{notion_prop_name_for_log}' (format: {expected_format_key}): {e}", exc_info=False)
        return "ERREUR_FORMAT"
    return ""

# --- Fonctions d'extraction de propriétés Notion (PREEXISTANTES ET PLUS GÉNÉRIQUES) ---
# Ceci est l'ancienne fonction extract_property_value qui est utilisée par fetch_notion_data
# pour les bases de données Ingrédients et Ingrédients_recettes.
def extract_property_value_generic(prop):
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
            extracted_items = []
            for item in rollup.get("array", []):
                if item.get("type") == "text":
                    extracted_items.append(item.get("text", {}).get("plain_text", ""))
                elif item.get("type") == "number":
                    extracted_items.append(str(item.get("number", "")))
            return ", ".join(extracted_items)
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
    api_timeout_seconds = 60 # Timeout pour la requête API

    filter_cond = json.loads(filter_json_str) if filter_json_str else {}

    logger.info(f"Début de l'extraction de la base de données Notion: {database_id}")

    while True:
        try:
            query_params = {
                "database_id": database_id,
                "page_size": BATCH_SIZE, # Utilise la constante globale BATCH_SIZE
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
                row_data = {"Page_ID": result.get("id", "")} # L'ID de la page Notion actuelle

                if columns_mapping:
                    for notion_prop, df_col in columns_mapping.items():
                        # Utilise la fonction générique pour les tables Ingrédients et Ingrédients_recettes
                        row_data[df_col] = extract_property_value_generic(properties.get(notion_prop, {}))
                else:
                    for prop_name, prop_data in properties.items():
                        row_data[prop_name] = extract_property_value_generic(prop_data)

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
        filter_json_str=json.dumps(filter_cond, sort_keys=True),
        columns_mapping=columns_mapping
    )

# Nouvelle fonction pour la base de données Ingrédients_recettes
def get_ingredients_recettes_data():
    filter_cond = {
        "property": "Type de stock f",
        "formula": {"string": {"equals": "Autre type"}}
    }
    columns_mapping = {
        "Elément parent": "Element_Parent_Relation_IDs",
        "Qté/pers_s": "Qté/pers_s",
        "Ingrédient ok": "Ingrédient ok",
        "Type de stock f": "Type de stock f"
    }

    df = fetch_notion_data(
        DATABASE_ID_INGREDIENTS_RECETTES,
        filter_json_str=json.dumps(filter_cond, sort_keys=True),
        columns_mapping=columns_mapping
    )

    if not df.empty and "Element_Parent_Relation_IDs" in df.columns:
        df['Page_ID_Formatted'] = df.apply(
            lambda row: row['Element_Parent_Relation_IDs'].split(',')[0].strip() if row['Element_Parent_Relation_IDs'] else row['Page_ID'],
            axis=1
        )
        df['Page_ID'] = df['Page_ID_Formatted']
        df = df.drop(columns=['Page_ID_Formatted', 'Element_Parent_Relation_IDs'])

        if 'Qté/pers_s' in df.columns:
            df['Qté/pers_s'] = pd.to_numeric(
                df['Qté/pers_s'].astype(str).str.replace(',', '.').replace('', '0'),
                errors='coerce'
            ).fillna(0)

            df = df[df['Qté/pers_s'] > 0]

    desired_columns = ["Page_ID", "Qté/pers_s", "Ingrédient ok", "Type de stock f"]
    existing_columns = [col for col in desired_columns if col in df.columns]
    df = df[existing_columns]

    return df

# ========== NOUVELLE FONCTION POUR RÉCUPÉRER LES DONNÉES DE LA BASE DE DONNÉES "RECETTES" ==========
@st.cache_data(show_spinner="Chargement des recettes depuis Notion...", ttl=3600)
def get_recettes_data():
    all_recettes_rows = []
    next_cursor = None
    total_extracted_from_api = 0
    start_time = time.time()

    filter_conditions = [
        {"property": "Elément parent", "relation": {"is_empty": True}},
        {
            "or": [
                {"property": "Saison", "multi_select": {"contains": "Toute l'année"}},
                *([{"property": "Saison", "multi_select": {"contains": SAISON_FILTRE}}] if SAISON_FILTRE else []),
                {"property": "Saison", "multi_select": {"is_empty": True}}
            ]
        },
        {
            "or": [
                {"property": "Type_plat", "multi_select": {"contains": "Salade"}},
                {"property": "Type_plat", "multi_select": {"contains": "Soupe"}},
                {"property": "Type_plat", "multi_select": {"contains": "Plat"}}
            ]
        }
    ]
    filter_recettes = {"and": filter_conditions}
    logger.info(f"Filtre API Notion pour Recettes : {filter_recettes}")

    # Noms de propriétés Notion vérifiés par rapport à votre liste
    csv_to_notion_mapping = {
        "Page_ID":          (None, "page_id_special"),
        "Nom":              ("Nom_plat", "title"),
        "ID_Recette":       ("ID_Recette", "unique_id_or_text"),
        "Saison":           ("Saison", "multi_select_comma_separated"),
        "Calories":         ("Calories Recette", "rollup_single_number_or_empty"),
        "Proteines":        ("Proteines Recette", "rollup_single_number_or_empty"),
        "Temps_total":      ("Temps_total", "formula_number_or_string_or_empty"),
        "Aime_pas_princip": ("Aime_pas_princip", "rollup_formula_string_dots_comma_separated"),
        "Type_plat":        ("Type_plat", "multi_select_comma_separated"),
        "Transportable":    ("Transportable", "select_to_oui_empty")
    }


    while total_extracted_from_api < NUM_ROWS_TO_EXTRACT:
        retries = 0
        current_retry_delay = RETRY_DELAY_INITIAL

        try:
            query_params = {"database_id": DATABASE_ID_RECETTES, "filter": filter_recettes, "page_size": BATCH_SIZE}
            if next_cursor: query_params["start_cursor"] = next_cursor

            logger.info(f"Appel API Notion pour Recettes (Curseur: {next_cursor or 'aucun'}).")
            response = notion.databases.query(**query_params)

            pages_batch = response.get("results", [])
            total_extracted_from_api += len(pages_batch)
            logger.info(f"API Recettes a retourné {len(pages_batch)} pages pour ce lot. (Total API: {total_extracted_from_api})")

            next_cursor = response.get("next_cursor")

        except (RequestTimeoutError, httpx.TimeoutException, httpx.ReadTimeout) as e:
            retries += 1; logger.warning(f"Timeout API Recettes (tentative {retries}/{MAX_RETRIES}). Attente {current_retry_delay}s...")
            if retries >= MAX_RETRIES: logger.error(f"Max timeouts atteints pour Recettes. Abandon."); break
            time.sleep(current_retry_delay); current_retry_delay = min(current_retry_delay*2, 60); continue
        except APIResponseError as e:
            logger.error(f"Erreur API Notion pour Recettes: {e.code} - {e.message}.")
            if e.code in ["validation_error", "invalid_json", "unauthorized", "restricted_resource"]: logger.error("Erreur API non récupérable pour Recettes. Abandon."); break
            retries += 1; logger.warning(f"Erreur API Recettes (tentative {retries}/{MAX_RETRIES}). Attente {current_retry_delay}s...")
            if retries >= MAX_RETRIES: logger.error(f"Max erreurs API atteintes pour Recettes. Abandon."); break
            time.sleep(current_retry_delay); current_retry_delay = min(current_retry_delay*2, 60); continue
        except Exception as e:
            logger.error(f"Erreur inattendue pour Recettes: {e}", exc_info=True); break

        if not pages_batch:
            if total_extracted_from_api == 0:
                 logger.critical("!!! AUCUNE PAGE RECETTE RETOURNÉE PAR L'API AVEC LE FILTRE ACTUEL !!!")
                 logger.critical("Cause probable : Noms de propriétés incorrects dans le filtre OU conditions du filtre trop restrictives OU permissions de l'intégration.")
                 logger.critical(f"Filtre utilisé : {filter_recettes}")
            else:
                 logger.info("Plus de pages Recettes à récupérer de l'API.")
            break

        for page in pages_batch:
            page_props_raw = page.get("properties", {})
            row_data = {}
            for csv_col_name, (notion_prop_name_key, expected_format_key) in csv_to_notion_mapping.items():
                if csv_col_name == "Page_ID":
                    row_data[csv_col_name] = page.get("id", "")
                else:
                    raw_prop_data = page_props_raw.get(notion_prop_name_key)
                    if raw_prop_data is None and notion_prop_name_key is not None:
                         logger.warning(f"Propriété Notion '{notion_prop_name_key}' (pour CSV '{csv_col_name}') non trouvée dans la page ID {page.get('id')}. Clés dispo: {list(page_props_raw.keys())}")
                    row_data[csv_col_name] = extract_recette_property_value(raw_prop_data, notion_prop_name_key, expected_format_key)
            all_recettes_rows.append(row_data)

        if not next_cursor or total_extracted_from_api >= NUM_ROWS_TO_EXTRACT:
            logger.info("Fin de l'extraction des recettes (plus de pages ou limite atteinte).")
            break
        time.sleep(0.35)

    if all_recettes_rows:
        df = pd.DataFrame(all_recettes_rows)
        logger.info(f"Extraction des recettes réussie : {len(df)} recettes chargées.")
        return df
    else:
        logger.info(f"Aucune recette extraite de {DATABASE_ID_RECETTES}.")
        return pd.DataFrame()


# --- Fonction Principale de l'Application Streamlit ---
def main():
    st.set_page_config(layout="wide", page_title="Générateur de Menus Notion")
    st.title("🍽️ Générateur de Menus Automatisé avec Notion")

    st.sidebar.header("Chargement des Données")

    # Bouton de réinitialisation/rechargement global pour Notion
    st.sidebar.markdown("---")
    st.sidebar.subheader("Actions de Rechargement")
    # Clarification pour l'utilisateur sur le CSV
    st.sidebar.info("Note : Le fichier 'Planning.csv' doit être rechargé manuellement via le bouton ci-dessous après une réinitialisation.")

    if st.sidebar.button("✨ Recharger toutes les données Notion", help="Vide le cache Streamlit et recharge toutes les données depuis Notion."):
        st.cache_data.clear() # Vide le cache des fonctions décorées
        # Ces DataFrames seront rechargés par les appels suivants à get_..._data()
        st.session_state['df_ingredients'] = pd.DataFrame()
        st.session_state['df_ingredients_recettes'] = pd.DataFrame()
        st.session_state['df_recettes'] = pd.DataFrame() # Nouvelle ligne
        st.success("Cache et DataFrames Notion réinitialisés. Rechargement des données...")
        # Forcer le rechargement via les fonctions d'obtention de données
        st.session_state['df_ingredients'] = get_ingredients_data()
        st.session_state['df_ingredients_recettes'] = get_ingredients_recettes_data()
        st.session_state['df_recettes'] = get_recettes_data() # Nouvelle ligne
        st.success("Toutes les données Notion ont été rechargées.")
        st.rerun() # Recharge l'application pour afficher les nouvelles données

    st.sidebar.markdown("---") # Séparateur visuel

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
    if 'df_ingredients_recettes' not in st.session_state:
        st.session_state['df_ingredients_recettes'] = pd.DataFrame()
    if 'df_recettes' not in st.session_state: # Nouvelle initialisation
        st.session_state['df_recettes'] = pd.DataFrame()

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

    # Chargement automatique des données Notion au démarrage ou si elles sont vides
    # Ces appels utiliseront le cache si les données sont déjà là, ou referont la requête sinon.
    if st.session_state['df_ingredients'].empty:
        st.session_state['df_ingredients'] = get_ingredients_data()
        if not st.session_state['df_ingredients'].empty:
            st.sidebar.success(f"Données Ingrédients (Notion) chargées ({len(st.session_state['df_ingredients'])} lignes).")
    if st.session_state['df_ingredients_recettes'].empty:
        st.session_state['df_ingredients_recettes'] = get_ingredients_recettes_data()
        if not st.session_state['df_ingredients_recettes'].empty:
            st.sidebar.success(f"Données Ingrédients par Recette (Notion) chargées ({len(st.session_state['df_ingredients_recettes'])} lignes).")
    if st.session_state['df_recettes'].empty: # Nouveau chargement auto pour Recettes
        st.session_state['df_recettes'] = get_recettes_data()
        if not st.session_state['df_recettes'].empty:
            st.sidebar.success(f"Données Recettes (Notion) chargées ({len(st.session_state['df_recettes'])} lignes).")

    # Affichage des statuts de chargement des données Notion
    st.sidebar.subheader("2. Statut des Données Notion")
    if not st.session_state['df_ingredients'].empty:
        st.sidebar.write(f"✅ Ingrédients : {len(st.session_state['df_ingredients'])} lignes.")
    else:
        st.sidebar.write("❌ Ingrédients : non chargé.")
    if not st.session_state['df_ingredients_recettes'].empty:
        st.sidebar.write(f"✅ Ingrédients/Recette : {len(st.session_state['df_ingredients_recettes'])} lignes.")
    else:
        st.sidebar.write("❌ Ingrédients/Recette : non chargé.")
    if not st.session_state['df_recettes'].empty:
        st.sidebar.write(f"✅ Recettes : {len(st.session_state['df_recettes'])} lignes.")
    else:
        st.sidebar.write("❌ Recettes : non chargé.")


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

    if not st.session_state['df_ingredients_recettes'].empty:
        st.write("✅ Données Ingrédients par Recette (Notion) chargées.")
        st.subheader("Aperçu de la table Ingrédients par Recette (Notion) :")
        st.dataframe(st.session_state['df_ingredients_recettes'].head())
    else:
        st.write("❌ Données Ingrédients par Recette (Notion) manquantes ou non chargées.")

    if not st.session_state['df_recettes'].empty:
        st.write("✅ Données Recettes (Notion) chargées.")
        st.subheader("Aperçu de la table Recettes (Notion) :")
        st.dataframe(st.session_state['df_recettes'].head())
    else:
        st.write("❌ Données Recettes (Notion) manquantes ou non chargées.")

    st.info("L'application charge maintenant le Planning.csv, les Ingrédients, les Ingrédients par Recette et les Recettes de Notion.")
    st.info("N'oubliez pas de configurer votre fichier `.streamlit/secrets.toml` avec les clés API et IDs de base de données nécessaires.")


if __name__ == "__main__":
    main()
