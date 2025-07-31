import streamlit as st
import pandas as pd
import logging
import time
import httpx
import io
import json
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError
from datetime import datetime # Import ajouté pour le traitement des dates

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

# --- Connexion à Notion et IDs des bases de données ---
try:
    NOTION_API_KEY = st.secrets["notion_api_key"]
    DATABASE_ID_INGREDIENTS = st.secrets["notion_database_id_ingredients"]
    DATABASE_ID_INGREDIENTS_RECETTES = st.secrets["notion_database_id_ingredients_recettes"]
    DATABASE_ID_RECETTES = st.secrets["notion_database_id_recettes"]
    DATABASE_ID_MENUS = st.secrets["notion_database_id_menus"] # ID pour la base de données des Menus

    notion = Client(auth=NOTION_API_KEY)
except KeyError as e:
    st.error(f"Le secret Notion manquant est : {e}. "
             "Veuillez configurer tous les secrets Notion dans le fichier .streamlit/secrets.toml "
             "ou via l'interface Streamlit Cloud. "
             "Assurez-vous d'avoir: notion_api_key, notion_database_id_ingredients, "
             "notion_database_id_ingredients_recettes, notion_database_id_recettes, "
             "notion_database_id_menus.")
    st.stop()

# ========== FONCTION D'EXTRACTION DE PROPRIÉTÉS SPÉCIFIQUE POUR LES RECETTES ==========
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

    filter_cond = json.loads(filter_json_str) if filter_json_str else {}

    logger.info(f"Début de l'extraction de la base de données Notion: {database_id}")

    while True:
        try:
            query_params = {
                "database_id": database_id,
                "page_size": BATCH_SIZE,
                "timeout": API_TIMEOUT_SECONDS,
            }
            if next_cursor:
                query_params["start_cursor"] = next_cursor
            if filter_cond:
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
                        row_data[df_col] = extract_property_value_generic(properties.get(notion_prop, {}))
                else:
                    for prop_name, prop_data in properties.items():
                        row_data[prop_name] = extract_property_value_generic(prop_data)

                all_rows.append(row_data)
                total_extracted += 1

            next_cursor = results.get("next_cursor")
            if not next_cursor:
                break
            time.sleep(0.1)

        except (httpx.TimeoutException, RequestTimeoutError) as e:
            logger.warning(f"Timeout détecté lors de la requête Notion ({database_id}): {e}. Réessai...")
            time.sleep(5)
            continue
        except Exception as e:
            logger.exception(f"Erreur inattendue lors de l'extraction Notion de {database_id}: {e}")
            st.error(f"Erreur lors de la récupération des données de Notion pour la base {database_id}: {e}")
            return pd.DataFrame()

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

    while total_extracted_from_api < NUM_ROWS_TO_EXTRACT: # Utilise NUM_ROWS_TO_EXTRACT global
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

# --- Fonctions de traitement des données existantes ---
def process_data(df_planning, df_recettes, df_ingredients, df_ingredients_recettes):
    st.info("Traitement des données en cours...")
    logger.info("Début du traitement des données.")

    # Nettoyage des noms de colonnes : suppression des espaces superflus et caractères spéciaux
    df_planning.columns = [col.strip().replace(" ", "_").replace("(", "").replace(")", "").replace("é", "e").replace("à", "a").replace("ç", "c").lower() for col in df_planning.columns]
    df_recettes.columns = [col.strip().replace(" ", "_").replace("(", "").replace(")", "").replace("é", "e").replace("à", "a").replace("ç", "c").lower() for col in df_recettes.columns]
    df_ingredients.columns = [col.strip().replace(" ", "_").replace("(", "").replace(")", "").replace("é", "e").replace("à", "a").replace("ç", "c").lower() for col in df_ingredients.columns]
    df_ingredients_recettes.columns = [col.strip().replace(" ", "_").replace("(", "").replace(")", "").replace("é", "e").replace("à", "a").replace("ç", "c").lower() for col in df_ingredients_recettes.columns]

    df_planning['date'] = pd.to_datetime(df_planning['date'], format='%d/%m/%Y')
    df_planning.set_index('date', inplace=True)

    df_recettes['nom'] = df_recettes['nom'].str.strip()

    df_ingredients_recettes['recette'] = df_ingredients_recettes['recette'].str.strip()
    df_ingredients_recettes['ingredient'] = df_ingredients_recettes['ingredient'].str.strip()

    df_ingredients['nom'] = df_ingredients['nom'].str.strip()

    # Fusion des dataframes
    df_menus = df_planning.stack().reset_index()
    df_menus.columns = ['date', 'repas_type', 'recette_nom']
    df_menus['date'] = df_menus['date'].dt.strftime('%d/%m/%Y')

    # Remplacer les valeurs vides ou "None" par une chaîne vide
    df_menus['recette_nom'] = df_menus['recette_nom'].fillna('').astype(str).str.strip()

    # Nettoyer les noms des colonnes 'repas_type' pour correspondre aux propriétés Notion
    df_menus['repas_type'] = df_menus['repas_type'].str.replace('_', ' ').str.title()
    df_menus['repas_type'] = df_menus['repas_type'].replace({
        'Dejeuner': 'Déjeuner',
        'Diner': 'Dîner'
    })

    df_menus_complet = pd.merge(df_menus, df_recettes, left_on='recette_nom', right_on='nom', how='left')
    df_menus_complet.rename(columns={'nom': 'Nom Recette', 'participants': 'Participant(s)'}, inplace=True)

    # Utilisation de DATABASE_ID_RECETTES pour chercher l'ID de la recette
    df_menus_complet['Recette ID'] = df_menus_complet['recette_nom'].apply(lambda x: get_page_id_by_name(DATABASE_ID_RECETTES, "Nom", x) if x else None) # Assurez-vous que "Nom" est la propriété de titre de votre base de recettes

    st.success("Traitement des données terminé.")
    logger.info("Fin du traitement des données.")

    return df_menus_complet, df_ingredients, df_ingredients_recettes

def generate_output_files(df_menus_complet, df_ingredients, df_ingredients_recettes):
    st.info("Génération des fichiers de sortie en cours...")
    logger.info("Début de la génération des fichiers de sortie.")

    # Préparation du DataFrame pour l'export CSV
    df_menu_genere = df_menus_complet[['date', 'Participant(s)', 'recette_nom']].copy()
    df_menu_genere.rename(columns={'date': 'Date', 'recette_nom': 'Nom'}, inplace=True)

    # Formater les dates pour Notion au format YYYY-MM-DD HH:MM
    # La date dans df_menu_genere est déjà au format DD/MM/YYYY
    # Pour l'export Notion, on peut ajouter une heure par défaut si nécessaire
    df_menu_genere['Date'] = pd.to_datetime(df_menu_genere['Date'], format="%d/%m/%Y", errors='coerce').dt.strftime('%Y-%m-%d %H:%M')

    # Exporter en CSV pour téléchargement
    csv_buffer = io.StringIO()
    df_menu_genere.to_csv(csv_buffer, index=False, encoding="utf-8-sig")
    csv_data = csv_buffer.getvalue().encode("utf-8-sig")
    st.download_button(
        label="Télécharger Menus_generes.csv",
        data=csv_data,
        file_name=FICHIER_SORTIE_MENU_CSV,
        mime="text/csv",
    )
    logger.info(f"Fichier CSV '{FICHIER_SORTIE_MENU_CSV}' prêt pour téléchargement.")

    # Génération du récapitulatif des ingrédients
    df_details_ingredients = pd.merge(df_menus_complet, df_ingredients_recettes, left_on='recette_nom', right_on='recette', how='inner')
    df_details_ingredients = pd.merge(df_details_ingredients, df_ingredients, left_on='ingredient', right_on='nom', how='inner', suffixes=('_recette', '_stock'))

    df_details_ingredients['quantite'] = pd.to_numeric(df_details_ingredients['quantite'], errors='coerce').fillna(0)

    # Calcul des quantités totales par ingrédient et unité
    liste_courses = df_details_ingredients.groupby(['ingredient', 'unite'])['quantite'].sum().reset_index()

    # Comparaison avec le stock (si la colonne 'quantite_stock' existe et est numérique)
    if 'quantite_stock' in df_ingredients.columns:
        df_ingredients['quantite_stock'] = pd.to_numeric(df_ingredients['quantite_stock'], errors='coerce').fillna(0)
        liste_courses = pd.merge(liste_courses, df_ingredients[['nom', 'quantite_stock']], left_on='ingredient', right_on='nom', how='left').drop(columns='nom')
        liste_courses['A acheter'] = liste_courses['quantite'] - liste_courses['quantite_stock']
        liste_courses['A acheter'] = liste_courses['A acheter'].apply(lambda x: max(0, x)) # Ne pas afficher de quantités négatives

        # Filtre pour n'afficher que ce qui est à acheter
        liste_courses = liste_courses[liste_courses['A acheter'] > 0]
        st.subheader("Liste de courses (éléments à acheter) :")
        contenu_fichier_recap_txt = ["Liste de courses (éléments à acheter) :\n"]
        for _, row in liste_courses.iterrows():
            line = f"- {row['A acheter']:.2f} {row['unite']} de {row['ingredient']}\n"
            contenu_fichier_recap_txt.append(line)
            st.write(line.strip()) # Afficher aussi dans l'app
    else:
        st.subheader("Récapitulatif des ingrédients requis (sans comparaison de stock) :")
        contenu_fichier_recap_txt = ["Récapitulatif des ingrédients requis :\n"]
        for _, row in liste_courses.iterrows():
            line = f"- {row['quantite']:.2f} {row['unite']} de {row['ingredient']}\n"
            contenu_fichier_recap_txt.append(line)
            st.write(line.strip()) # Afficher aussi dans l'app

    txt_buffer = io.StringIO()
    txt_buffer.writelines(contenu_fichier_recap_txt)
    txt_data = txt_buffer.getvalue().encode("utf-8")
    st.download_button(
        label="Télécharger Liste_ingredients.txt",
        data=txt_data,
        file_name=FICHIER_SORTIE_LISTES_TXT,
        mime="text/plain",
    )
    logger.info(f"Fichier TXT '{FICHIER_SORTIE_LISTES_TXT}' prêt pour téléchargement.")
    st.success("Génération des fichiers de sortie terminée.")

# --- Fonction d'intégration Notion (à adapter si nécessaire) ---
def integrate_with_notion(df_menus_complet):
    st.info("Intégration avec Notion en cours...")
    logger.info("Début de l'intégration avec Notion.")

    # Filtrer les lignes qui n'ont pas de recette_nom vide
    df_to_integrate = df_menus_complet[df_menus_complet['recette_nom'] != ''].copy()

    if df_to_integrate.empty:
        st.warning("Aucun menu valide à intégrer dans Notion.")
        logger.warning("Aucun menu valide à intégrer dans Notion.")
        return

    # Vérifier l'existence des pages de recettes dans Notion pour récupérer les IDs de relation
    recette_ids = {}
    st.info("Vérification des recettes existantes dans Notion...")
    for recette_nom in df_to_integrate['recette_nom'].unique():
        # Utilisation de DATABASE_ID_RECETTES pour chercher la recette
        page_id = get_page_id_by_name(DATABASE_ID_RECETTES, "Nom", recette_nom)
        if page_id:
            recette_ids[recette_nom] = page_id
        else:
            st.warning(f"La recette '{recette_nom}' n'a pas été trouvée dans Notion. Elle ne sera pas liée.")

    for index, row in df_to_integrate.iterrows():
        date_str = row['date']
        repas_type = row['repas_type']
        recette_nom = row['recette_nom']
        participants = row['Participant(s)']

        properties = {
            "Date": {
                "date": {
                    "start": datetime.strptime(date_str, '%d/%m/%Y').isoformat()
                }
            },
            "Repas": {
                "select": {
                    "name": repas_type
                }
            },
            "Nom": {
                "title": [
                    {
                        "text": {
                            "content": f"{repas_type} - {recette_nom} ({date_str})"
                        }
                    }
                ]
            },
            "Participant(s)": {
                "rich_text": [
                    {
                        "text": {
                            "content": str(participants)
                        }
                    }
                ]
            }
        }

        if recette_nom in recette_ids:
            properties["Recette"] = {
                "relation": [{"id": recette_ids[recette_nom]}]
            }
        else:
            st.warning(f"Impossible de lier la recette '{recette_nom}' pour le {repas_type} du {date_str} car elle n'a pas été trouvée dans Notion.")

        existing_page_id = get_page_id_by_name(DATABASE_ID_MENUS, "Nom", properties["Nom"]["title"][0]["text"]["content"])

        if existing_page_id:
            st.info(f"La page pour '{repas_type} - {recette_nom} ({date_str})' existe déjà. Mise à jour en cours...")
            pass
        else:
            st.info(f"Création de la page pour '{repas_type} - {recette_nom} ({date_str})'...")
            create_page(DATABASE_ID_MENUS, properties)
    st.success("Intégration avec Notion terminée.")
    logger.info("Fin de l'intégration avec Notion.")

# --- Nouvelle Fonction d'extraction des menus existants depuis Notion ---
@st.cache_data(show_spinner="Extraction des menus existants depuis Notion...", ttl=3600)
def get_existing_menus_data():
    all_menus_rows = []
    next_cursor = None
    total_extracted_from_api = 0

    # Noms de propriétés Notion pour la base de données des Menus
    nom_menu_property_name = "Nom Menu"
    recette_property_name = "Recette"
    date_property_name = "Date"

    filter_menus = {"property": recette_property_name, "relation": {"is_not_empty": True}}
    logger.info(f"Filtre API Notion pour Menus existants : {filter_menus}")

    while total_extracted_from_api < NUM_ROWS_TO_EXTRACT:
        retries = 0
        current_retry_delay = RETRY_DELAY_INITIAL

        try:
            query_params = {
                "database_id": DATABASE_ID_MENUS,
                "filter": filter_menus,
                "page_size": BATCH_SIZE,
                "timeout": API_TIMEOUT_SECONDS
            }
            if next_cursor: query_params["start_cursor"] = next_cursor

            logger.info(f"Appel API Notion pour Menus (Curseur: {next_cursor or 'aucun'}).")
            response = notion.databases.query(**query_params)

            pages_batch = response.get("results", [])
            total_extracted_from_api += len(pages_batch)
            logger.info(f"API Menus a retourné {len(pages_batch)} pages pour ce lot. (Total API: {total_extracted_from_api})")

            next_cursor = response.get("next_cursor")

        except (RequestTimeoutError, httpx.TimeoutException, httpx.ReadTimeout) as e:
            retries += 1; logger.warning(f"Timeout API Menus (tentative {retries}/{MAX_RETRIES}). Attente {current_retry_delay}s...")
            if retries >= MAX_RETRIES: logger.error(f"Max timeouts atteints pour Menus. Abandon."); break
            time.sleep(current_retry_delay); current_retry_delay = min(current_retry_delay*2, 60); continue
        except APIResponseError as e:
            logger.error(f"Erreur API Notion pour Menus: {e.code} - {e.message}.")
            if e.code in ["validation_error", "invalid_json", "unauthorized", "restricted_resource"]: logger.error("Erreur API non récupérable pour Menus. Abandon."); break
            retries += 1; logger.warning(f"Erreur API Menus (tentative {retries}/{MAX_RETRIES}). Attente {current_retry_delay}s...")
            if retries >= MAX_RETRIES: logger.error(f"Max erreurs API atteintes pour Menus. Abandon."); break
            time.sleep(current_retry_delay); current_retry_delay = min(current_retry_delay*2, 60); continue
        except Exception as e:
            logger.error(f"Erreur inattendue pour Menus: {e}", exc_info=True); break

        if not pages_batch:
            if total_extracted_from_api == 0:
                 logger.critical("!!! AUCUNE PAGE MENU RETOURNÉE PAR L'API AVEC LE FILTRE ACTUEL !!!")
                 logger.critical("Cause probable : Noms de propriétés incorrects dans le filtre OU conditions du filtre trop restrictives OU permissions de l'intégration.")
                 logger.critical(f"Filtre utilisé : {filter_menus}")
            else:
                 logger.info("Plus de pages Menus à récupérer de l'API.")
            break

        for page in pages_batch:
            properties = page.get("properties", {})
            nom_menu_value = ""
            if nom_menu_property_name in properties:
                name_property = properties[nom_menu_property_name]
                nom_menu_value = "".join([text.get("plain_text", "") for text in name_property.get("title", []) or name_property.get("rich_text", [])])

            recette_value = ""
            if recette_property_name in properties:
                prop = properties[recette_property_name]
                if prop["type"] == "relation" and prop["relation"]:
                    recette_value = ", ".join([relation["id"] for relation in prop["relation"]])
                elif prop["type"] == "rollup" and prop["rollup"]:
                    rollup_data = prop["rollup"]
                    if rollup_data.get("type") == "array" and rollup_data.get("array"):
                        recette_ids = []
                        for item in rollup_data["array"]:
                            if item.get("id"):
                                recette_ids.append(item["id"])
                            elif item.get("relation"):
                                recette_ids.extend([rel.get("id") for rel in item["relation"] if rel.get("id")])
                        recette_value = ", ".join(recette_ids)

            date_value = ""
            if date_property_name in properties:
                date_property = properties[date_property_name]
                if date_property["type"] == "date" and date_property.get("date") and date_property["date"].get("start"):
                    date_str = date_property["date"]["start"]
                    date_object = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                    date_value = date_object.strftime('%Y-%m-%d')

            all_menus_rows.append({
                "Nom Menu": nom_menu_value.strip(),
                "Recette": recette_value,
                "Date": date_value
            })

        if not next_cursor or total_extracted_from_api >= NUM_ROWS_TO_EXTRACT:
            logger.info("Fin de l'extraction des menus existants (plus de pages ou limite atteinte).")
            break
        time.sleep(0.35)

    if all_menus_rows:
        df = pd.DataFrame(all_menus_rows)
        logger.info(f"Extraction des menus existants réussie : {len(df)} menus chargés.")
        return df
    else:
        logger.info(f"Aucun menu extrait de {DATABASE_ID_MENUS}.")
        return pd.DataFrame()


# --- Application Streamlit principale ---
def main():
    st.set_page_config(layout="wide", page_title="Générateur de Menus Notion")
    st.title("🍽️ Générateur de Menus pour Notion")

    st.markdown("""
    Cette application vous permet de générer des menus, des listes d'ingrédients,
    et de les synchroniser avec votre base de données Notion "Planning Menus".
    """)

    st.sidebar.header("Chargement des Données")

    # Bouton de réinitialisation/rechargement global pour Notion
    st.sidebar.markdown("---")
    st.sidebar.subheader("Actions de Rechargement")
    st.sidebar.info("Note : Le fichier 'Planning.csv' doit être rechargé manuellement via le bouton ci-dessous après une réinitialisation.")

    if st.sidebar.button("✨ Recharger toutes les données Notion", help="Vide le cache Streamlit et recharge toutes les données depuis Notion."):
        st.cache_data.clear() # Vide le cache des fonctions décorées
        # Ces DataFrames seront rechargés par les appels suivants à get_..._data()
        st.session_state['df_ingredients'] = pd.DataFrame()
        st.session_state['df_ingredients_recettes'] = pd.DataFrame()
        st.session_state['df_recettes'] = pd.DataFrame()
        st.session_state['df_menus_notion'] = pd.DataFrame() # Nouvelle ligne pour les menus existants

        st.success("Cache et DataFrames Notion réinitialisés. Rechargement des données...")
        # Forcer le rechargement via les fonctions d'obtention de données
        with st.spinner("Rechargement des ingrédients..."):
            st.session_state['df_ingredients'] = get_ingredients_data()
        with st.spinner("Rechargement des ingrédients par recette..."):
            st.session_state['df_ingredients_recettes'] = get_ingredients_recettes_data()
        with st.spinner("Rechargement des recettes..."):
            st.session_state['df_recettes'] = get_recettes_data()
        with st.spinner("Rechargement des menus existants..."):
            st.session_state['df_menus_notion'] = get_existing_menus_data() # Nouvelle ligne

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
    if 'df_recettes' not in st.session_state:
        st.session_state['df_recettes'] = pd.DataFrame()
    if 'df_menus_notion' not in st.session_state: # Initialisation pour les menus existants
        st.session_state['df_menus_notion'] = pd.DataFrame()

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
    if st.session_state['df_ingredients_recettes'].empty:
        st.session_state['df_ingredients_recettes'] = get_ingredients_recettes_data()
    if st.session_state['df_recettes'].empty:
        st.session_state['df_recettes'] = get_recettes_data()
    if st.session_state['df_menus_notion'].empty: # Chargement auto pour les menus existants
        st.session_state['df_menus_notion'] = get_existing_menus_data()


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
    if not st.session_state['df_menus_notion'].empty: # Statut pour les menus existants
        st.sidebar.write(f"✅ Menus existants : {len(st.session_state['df_menus_notion'])} lignes.")
    else:
        st.sidebar.write("❌ Menus existants : non chargé.")


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

    if not st.session_state['df_menus_notion'].empty: # Affichage des menus existants
        st.write("✅ Données Menus existants (Notion) chargées.")
        st.subheader("Aperçu de la table Menus existants (Notion) :")
        st.dataframe(st.session_state['df_menus_notion'].head())
    else:
        st.write("❌ Données Menus existants (Notion) manquantes ou non chargées.")


    st.header("2. Générer les menus et listes")
    # Condition pour activer le bouton de génération
    if all(df is not None and not df.empty for df in [st.session_state['df_planning'], st.session_state['df_recettes'], st.session_state['df_ingredients'], st.session_state['df_ingredients_recettes']]):
        if st.button("Générer les Menus et Listes"):
            with st.spinner("Génération en cours..."):
                df_menus_complet, df_ingredients_processed, df_ingredients_recettes_processed = process_data(
                    st.session_state['df_planning'], st.session_state['df_recettes'],
                    st.session_state['df_ingredients'], st.session_state['df_ingredients_recettes']
                )

                if not df_menus_complet.empty:
                    st.subheader("Aperçu des Menus Générés :")
                    st.dataframe(df_menus_complet[['date', 'repas_type', 'recette_nom', 'Participant(s)']])

                    generate_output_files(df_menus_complet, df_ingredients_processed, df_ingredients_recettes_processed)

                    st.header("3. Intégrer avec Notion")
                    notion_integrate = st.checkbox("Envoyer les menus générés à Notion?")
                    if notion_integrate:
                        if st.button("Lancer l'intégration Notion"):
                            with st.spinner("Intégration Notion en cours..."):
                                integrate_with_notion(df_menus_complet)
                                st.success("Processus d'intégration Notion terminé.")
                else:
                    st.warning("Aucun menu n'a pu être généré. Veuillez vérifier vos fichiers.")
    else:
        st.warning("Veuillez charger tous les fichiers CSV nécessaires et les données Notion pour activer la génération.")


    st.header("4. Extraire les Menus existants depuis Notion")
    st.markdown("Cette section vous permet de télécharger un fichier CSV contenant les menus actuellement enregistrés dans votre base de données Notion.")

    if st.button("Extraire et Télécharger les Menus de Notion"):
        with st.spinner("Extraction en cours depuis Notion..."):
            csv_data_extracted = get_existing_menus_data() # Appelle la fonction qui retourne déjà les données
            if csv_data_extracted is not None and not csv_data_extracted.empty:
                # Convertir le DataFrame en CSV pour le téléchargement
                csv_buffer = io.StringIO()
                csv_data_extracted.to_csv(csv_buffer, index=False, encoding="utf-8-sig")
                csv_bytes = csv_buffer.getvalue().encode("utf-8-sig")

                st.download_button(
                    label="Télécharger Menus_extraits_Notion.csv",
                    data=csv_bytes,
                    file_name=FICHIER_EXPORT_NOTION_CSV,
                    mime="text/csv",
                )
                st.success("Fichier d'extraction Notion prêt au téléchargement.")
            else:
                st.error("L'extraction des menus existants depuis Notion a échoué ou n'a retourné aucune donnée.")


    st.info("N'oubliez pas de configurer vos secrets Notion dans Streamlit Cloud.")


if __name__ == "__main__":
    main()
