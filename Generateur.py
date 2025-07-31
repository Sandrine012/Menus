import streamlit as st
import pandas as pd
import logging
import re
from datetime import datetime, timedelta
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError
import httpx
import io
import unicodedata # For remove_accents
import time # For time.sleep
import random # For random.choice

# --- Configuration du logger ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Constantes pour les noms de fichiers de sortie (en mémoire pour Streamlit) ---
FICHIER_SORTIE_MENU_CSV = "Menus_generes.csv"
FICHIER_SORTIE_LISTES_TXT = "Listes_ingredients.txt"

# --- Paramètres Notion (récupérés via Streamlit secrets) ---
NOTION_API_KEY = None
DATABASE_ID_INGREDIENTS = None
DATABASE_ID_INGREDIENTS_RECETTES = None
DATABASE_ID_RECETTES = None
DATABASE_ID_MENUS = None
notion_client = None

try:
    NOTION_API_KEY = st.secrets["notion_api_key"]
    # Utilisation des IDs hardcodés dans le script fourni comme valeurs par défaut si non trouvés dans secrets
    DATABASE_ID_INGREDIENTS = st.secrets.get("notion_database_id_ingredients", "b23b048b67334032ac1ae4e82d308817")
    DATABASE_ID_INGREDIENTS_RECETTES = st.secrets.get("notion_database_id_ingredients_recettes", "1d16fa46f8b2805b8377eba7bf668eb5")
    DATABASE_ID_RECETTES = st.secrets.get("notion_database_id_recettes", "1d16fa46f8b2805b8377eba7bf668eb5") # Same as Ingredients_recettes in original code
    DATABASE_ID_MENUS = st.secrets.get("notion_database_id_menus", "9025cfa1c18d4501a91dbeb1b10b48bd")

    notion_client = Client(auth=NOTION_API_KEY)
    st.sidebar.success("Connexion Notion configurée.")
except KeyError:
    st.sidebar.error("Les secrets Notion (notion_api_key et/ou les IDs de bases de données) ne sont pas configurés. "
                     "Veuillez les ajouter dans le fichier .streamlit/secrets.toml ou via Streamlit Cloud.")
    st.stop() # Arrête l'exécution de l'application si les secrets ne sont pas configurés.
except Exception as e:
    st.sidebar.error(f"Erreur lors de l'initialisation du client Notion : {e}")
    st.stop()

# --- Fonctions d'aide pour l'extraction de propriétés Notion ---
def extract_property_value(prop_data, notion_prop_name_for_log=None, expected_format_key=None):
    if not isinstance(prop_data, dict):
        return ""
    
    # Generic extraction based on type
    t = prop_data.get("type")
    if t == "title":
        return "".join([text.get("plain_text", "") for text in prop_data.get("title", [])])
    elif t == "rich_text":
        return "".join([text.get("plain_text", "") for text in prop_data.get("rich_text", [])])
    elif t == "multi_select":
        return ", ".join([opt.get("name", "") for opt in prop_data.get("multi_select", [])])
    elif t == "select":
        select_obj = prop_data.get("select")
        if select_obj is not None:
            return select_obj.get("name", "")
        return ""
    elif t == "number":
        num_val = prop_data.get("number")
        return str(num_val) if num_val is not None else ""
    elif t == "checkbox":
        return str(prop_data.get("checkbox", ""))
    elif t == "date":
        date_obj = prop_data.get("date")
        if date_obj is not None:
            return date_obj.get("start", "")
        return ""
    elif t == "people":
        return ", ".join([person.get("name", "") for person in prop_data.get("people", [])])
    elif t == "relation":
        # For relations, typically we want the ID. This is a common extraction.
        return ", ".join([rel.get("id", "") for rel in prop_data.get("relation", [])])
    elif t == "url":
        return prop_data.get("url", "")
    elif t == "email":
        return prop_data.get("email", "")
    elif t == "phone_number":
        return prop_data.get("phone_number", "")
    elif t == "formula":
        formula = prop_data.get("formula", {})
        if formula.get("type") == "string":
            return formula.get("string", "")
        elif formula.get("type") == "number":
            num_val = formula.get("number")
            return str(num_val) if num_val is not None else ""
        elif formula.get("type") == "boolean":
            return str(formula.get("boolean", ""))
    elif t == "rollup":
        rollup = prop_data.get("rollup", {})
        if rollup.get("type") == "array":
            # Attempt to extract text or number from array items
            values = []
            for item in rollup.get("array", []):
                if item.get("type") == "rich_text":
                    values.append("".join(t.get("plain_text", "") for t in item.get("rich_text", [])))
                elif item.get("type") == "title":
                    values.append("".join(t.get("plain_text", "") for t in item.get("title", [])))
                elif item.get("type") == "number":
                    values.append(str(item.get("number", "")))
                elif item.get("type") == "formula":
                    f_val = item.get("formula", {})
                    if f_val.get("type") == "string": values.append(f_val.get("string", ""))
                    elif f_val.get("type") == "number": values.append(str(f_val.get("number", "")))
            return ", ".join(filter(None, values))
        elif rollup.get("type") in ["number", "string"]: # Single value rollup
            val = rollup.get(rollup.get("type"))
            return str(val) if val is not None else ""
    elif t == "unique_id":
        uid = prop_data.get("unique_id", {}); p, n = uid.get("prefix"), uid.get("number")
        return f"{p}-{n}" if p and n is not None else (str(n) if n is not None else "")
    return ""


# --- Fonctions d'extraction des données Notion ---
@st.cache_data(ttl=3600) # Cache les données pendant 1 heure
def fetch_notion_data(database_id, filter_conditions=None, csv_header_map=None, custom_extract_logic=None):
    all_rows = []
    next_cursor = None
    total_extracted = 0
    batch_size = 50 # Increased batch size
    api_timeout_seconds = 180
    max_retries = 7
    retry_delay_initial = 5
    retries = 0

    if csv_header_map is None:
        st.error("L'en-tête CSV et le mapping des propriétés Notion sont requis.")
        return pd.DataFrame()

    with st.spinner(f"Extraction des données depuis Notion (DB ID: {database_id})..."):
        try:
            while True:
                try:
                    query_params = {"database_id": database_id, "page_size": batch_size}
                    if filter_conditions:
                        query_params["filter"] = filter_conditions
                    if next_cursor:
                        query_params["start_cursor"] = next_cursor

                    results = notion_client.databases.query(**query_params, timeout=api_timeout_seconds)
                    page_results = results.get("results", [])
                    retries = 0 # Reset retries on success

                    if not page_results:
                        logger.info("Aucun résultat retourné par l'API ou fin de la base de données atteinte.")
                        break

                    for result in page_results:
                        row_values = []
                        properties = result.get("properties", {})
                        for csv_col, (notion_prop_key, expected_type_hint) in csv_header_map.items():
                            if csv_col == "Page_ID":
                                row_values.append(result.get("id", ""))
                            else:
                                raw_prop_data = properties.get(notion_prop_key)
                                if raw_prop_data is None and notion_prop_key is not None:
                                    logger.warning(f"Propriété Notion '{notion_prop_key}' (pour CSV '{csv_col}') non trouvée dans la page ID {result.get('id')}. Clés dispo: {list(properties.keys())}")
                                
                                # Use custom extraction logic if provided for specific columns, otherwise generic
                                if custom_extract_logic and notion_prop_key in custom_extract_logic:
                                    value = custom_extract_logic[notion_prop_key](raw_prop_data)
                                else:
                                    value = extract_property_value(raw_prop_data)
                                
                                row_values.append(value)
                        all_rows.append(row_values)
                        total_extracted += 1

                    next_cursor = results.get("next_cursor")
                    if not next_cursor:
                        break
                    st.sidebar.text(f"  Pages extraites: {total_extracted}...")
                    time.sleep(0.1) # Shorter pause for faster extraction but still avoid rate limiting

                except (httpx.TimeoutException, RequestTimeoutError) as e:
                    retries += 1
                    logger.warning(f"Timeout API (tentative {retries}/{max_retries}). Attente {retry_delay_initial}s...")
                    if retries >= max_retries:
                        st.error(f"Nombre maximum de tentatives atteint après Timeout. Abandon de l'extraction de la base {database_id}.")
                        break
                    time.sleep(retry_delay_initial)
                    retry_delay_initial = min(retry_delay_initial * 2, 60) # Exponential backoff
                    continue # Try fetching the same page again
                except APIResponseError as e:
                    logger.error(f"Erreur API Notion pour la base {database_id}: {e.code} - {e.message}.")
                    if e.code in ["validation_error", "unauthorized", "restricted_resource"]:
                        st.error(f"Erreur API non récupérable pour la base {database_id}. Veuillez vérifier les permissions ou l'ID de la base de données.")
                        break
                    retries += 1
                    if retries >= max_retries:
                        st.error(f"Nombre maximum d'erreurs API atteint pour la base {database_id}. Abandon.")
                        break
                    time.sleep(retry_delay_initial)
                    retry_delay_initial = min(retry_delay_initial * 2, 60)
                    continue
                except Exception as e:
                    st.error(f"Une erreur inattendue est survenue lors de l'extraction de Notion pour la base {database_id}: {e}")
                    logger.exception(f"Erreur inattendue lors de l'extraction de Notion pour la base {database_id}")
                    break

        except Exception as e:
            st.error(f"Erreur générale lors de la préparation de l'extraction Notion pour la base {database_id}: {e}")
            logger.exception(f"Erreur générale lors de la préparation de l'extraction Notion pour la base {database_id}")

    if not all_rows:
        st.warning(f"Aucune donnée extraite de la base de données Notion ID: {database_id}. Vérifiez les filtres ou les permissions.")
        return pd.DataFrame(columns=[col for col, _ in csv_header_map.items()])

    df = pd.DataFrame(all_rows, columns=[col for col, _ in csv_header_map.items()])
    st.success(f"Extraction terminée : {len(df)} lignes de la base de données Notion (ID: {database_id}).")
    return df

# --- Specific fetch functions using the generic one ---

def fetch_ingredients_data():
    csv_to_notion_mapping = {
        "Page_ID": (None, "page_id_special"),
        "Nom": ("Nom", "title"),
        "Type de stock": ("Type de stock", "select"),
        "unité": ("unité", "rich_text"),
        "Qte reste": ("Qte reste", "number")
    }
    filter_cond = {
        "property": "Type de stock",
        "select": {"equals": "Autre type"}
    }
    return fetch_notion_data(DATABASE_ID_INGREDIENTS, filter_cond, csv_to_notion_mapping)

def fetch_ingredients_recettes_data():
    csv_to_notion_mapping = {
        "Page_ID": (None, "page_id_special"),
        "Qté/pers_s": ("Qté/pers_s", "rich_text"),
        "Ingrédient ok": ("Ingrédient ok", "title"),
        "Type de stock f": ("Type de stock f", "formula"),
        "Elément parent": ("Elément parent", "relation")
    }

    # Custom extraction logic for specific columns
    custom_extract_logic = {
        "Elément parent": lambda prop_data: ", ".join([rel.get("id", "") for rel in prop_data.get("relation", [])]) if prop_data and prop_data.get("type") == "relation" else "",
        "Qté/pers_s": lambda prop_data: str(extract_property_value(prop_data)).replace(",", ".") if prop_data else "",
        "Type de stock f": lambda prop_data: extract_property_value(prop_data) # Formula string
    }

    filter_cond = {
        "property": "Type de stock f",
        "formula": {"string": {"equals": "Autre type"}}
    }
    df = fetch_notion_data(DATABASE_ID_INGREDIENTS_RECETTES, filter_cond, csv_to_notion_mapping, custom_extract_logic)

    # Post-processing for 'Elément parent' to match original script's logic
    if not df.empty and "Elément parent" in df.columns:
        df["Qté/pers_s"] = pd.to_numeric(df["Qté/pers_s"], errors='coerce')
        df = df[df["Qté/pers_s"] > 0].copy() # Use .copy() to avoid SettingWithCopyWarning
        df.rename(columns={"Elément parent": "Page_ID_Recette"}, inplace=True)
        if "Page_ID" in df.columns: # Drop Notion Page_ID as it's not the relation ID
            df.drop(columns=["Page_ID"], inplace=True)
    return df


def fetch_recettes_data(saison_filtre):
    csv_to_notion_mapping = {
        "Page_ID":              (None, "page_id_special"),
        "Nom":                  ("Nom_plat", "title"),
        "ID_Recette":           ("ID_Recette", "unique_id"), # Changed to unique_id
        "Saison":               ("Saison", "multi_select"),
        "Calories":             ("Calories Recette", "rollup"),
        "Proteines":            ("Proteines Recette", "rollup"),
        "Temps_total":          ("Temps_total", "formula"),
        "Aime_pas_princip":     ("Aime_pas_princip", "rollup"),
        "Type_plat":            ("Type_plat", "multi_select"),
        "Transportable":        ("Transportable", "select")
    }

    # Custom extraction logic for Recettes as needed
    custom_extract_logic = {
        "ID_Recette": lambda prop_data: extract_property_value(prop_data), # Uses the generic unique_id logic
        "Calories": lambda prop_data: extract_property_value(prop_data), # Generic rollup number/array
        "Proteines": lambda prop_data: extract_property_value(prop_data), # Generic rollup number/array
        "Temps_total": lambda prop_data: extract_property_value(prop_data), # Generic formula
        "Aime_pas_princip": lambda prop_data: extract_property_value(prop_data), # Generic rollup array
        "Transportable": lambda prop_data: "Oui" if extract_property_value(prop_data).lower() == "oui" else "", # Specific for "Oui" checkbox/select
    }

    filter_conditions = [
        {"property": "Elément parent", "relation": {"is_empty": True}}, # Filter out sub-recipes
        {
            "or": [
                {"property": "Saison", "multi_select": {"contains": "Toute l'année"}},
                *([{"property": "Saison", "multi_select": {"contains": saison_filtre}}] if saison_filtre else []),
                {"property": "Saison", "multi_select": {"is_empty": True}} # Include recipes with no season specified
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
    filter_recettes_notion = {"and": filter_conditions}
    return fetch_notion_data(DATABASE_ID_RECETTES, filter_recettes_notion, csv_to_notion_mapping, custom_extract_logic)

def fetch_menus_data():
    csv_to_notion_mapping = {
        "Nom Menu": ("Nom Menu", "title"),
        "Recette": ("Recette", "relation"),
        "Date": ("Date", "date")
    }

    custom_extract_logic = {
        "Recette": lambda prop_data: ", ".join([relation["id"] for relation in prop_data["relation"]]) if prop_data and prop_data.get("type") == "relation" else "",
        "Date": lambda prop_data: datetime.fromisoformat(prop_data["date"]["start"].replace('Z', '+00:00')).strftime('%Y-%m-%d') if prop_data and prop_data.get("type") == "date" and prop_data["date"].get("start") else ""
    }

    filter_cond = {
        "and": [
            {"property": "Recette", "relation": {"is_not_empty": True}},
        ]
    }
    return fetch_notion_data(DATABASE_ID_MENUS, filter_cond, csv_to_notion_mapping, custom_extract_logic)


# --- Fonctions de traitement des données (adaptées du script fourni) ---
def remove_accents(input_str):
    if not isinstance(input_str, str):
        return input_str
    nfkd_form = unicodedata.normalize('NFKD', input_str)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])

def clean_column_names(df):
    new_columns = {}
    for col in df.columns:
        cleaned_col = remove_accents(col.lower())
        # Replace spaces and special characters with underscore, then remove repeated underscores
        cleaned_col = re.sub(r'[^a-z0-9_]+', '', cleaned_col.replace(' ', '_'))
        new_columns[col] = cleaned_col
    return df.rename(columns=new_columns)

def process_data(df_planning, df_recettes_raw, df_ingredients_raw, df_ingredients_recettes_raw, df_menus_historique_notion, nb_jours_anti_repetition=42):
    st.info("Nettoyage et préparation des données...")

    # Assurez-vous que les DataFrames ne sont pas vides
    if df_planning.empty or df_recettes_raw.empty or df_ingredients_raw.empty or df_ingredients_recettes_raw.empty:
        st.error("Un ou plusieurs DataFrames d'entrée sont vides. Vérifiez le chargement des fichiers ou l'extraction Notion.")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    # Nettoyage des noms de colonnes pour tous les DataFrames
    df_planning = clean_column_names(df_planning)
    df_recettes = clean_column_names(df_recettes_raw)
    df_ingredients = clean_column_names(df_ingredients_raw)
    df_ingredients_recettes = clean_column_names(df_ingredients_recettes_raw)

    # Assurer que les colonnes nécessaires existent après le nettoyage
    required_planning_cols = ['date', 'repas_type', 'participant_s_'] # 'participant_s_' for 'Participant(s)'
    for col in required_planning_cols:
        if col not in df_planning.columns:
            st.error(f"La colonne '{col}' est manquante dans le fichier Planning.csv après nettoyage. Colonnes trouvées: {df_planning.columns.tolist()}")
            return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    required_recettes_cols = ['page_id', 'nom', 'type_plat', 'saison']
    for col in required_recettes_cols:
        if col not in df_recettes.columns:
            st.error(f"La colonne '{col}' est manquante dans les données Recettes après nettoyage. Colonnes trouvées: {df_recettes.columns.tolist()}")
            return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    required_ingredients_cols = ['page_id', 'nom', 'type_de_stock', 'qte_reste', 'unite']
    for col in required_ingredients_cols:
        if col not in df_ingredients.columns:
            st.error(f"La colonne '{col}' est manquante dans les données Ingrédients après nettoyage. Colonnes trouvées: {df_ingredients.columns.tolist()}")
            return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    required_ingredients_recettes_cols = ['page_id_recette', 'qte_pers_s', 'ingredient_ok']
    for col in required_ingredients_recettes_cols:
        if col not in df_ingredients_recettes.columns:
            st.error(f"La colonne '{col}' est manquante dans les données Ingredients_recettes après nettoyage. Colonnes trouvées: {df_ingredients_recettes.columns.tolist()}")
            return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()


    # Prétraitement de df_planning
    df_planning['date'] = pd.to_datetime(df_planning['date'])
    df_planning['annee'] = df_planning['date'].dt.year
    df_planning['semaine'] = df_planning['date'].dt.isocalendar().week.astype(int)
    df_planning['jour_semaine'] = df_planning['date'].dt.day_name(locale='fr_FR')
    df_planning['participant_s_'] = df_planning['participant_s_'].fillna(0).astype(int)

    # Prétraitement de df_recettes
    df_recettes['type_plat'] = df_recettes['type_plat'].apply(lambda x: x.split(', ') if isinstance(x, str) else [])
    df_recettes_exploded = df_recettes.explode('type_plat')

    # Prétraitement de df_ingredients (assurez-vous que 'nom' est unique pour le merge)
    df_ingredients_processed = df_ingredients.drop_duplicates(subset=['nom']).copy()
    df_ingredients_processed['nom'] = df_ingredients_processed['nom'].str.strip()
    df_ingredients_processed['qte_reste'] = pd.to_numeric(df_ingredients_processed['qte_reste'], errors='coerce').fillna(0)


    # Prétraitement de df_ingredients_recettes (assurez-vous des types numériques)
    df_ingredients_recettes['qte_pers_s'] = pd.to_numeric(df_ingredients_recettes['qte_pers_s'], errors='coerce').fillna(0)
    df_ingredients_recettes_processed = df_ingredients_recettes.copy()
    # Merge df_ingredients_recettes avec df_ingredients pour obtenir les noms d'ingrédients
    df_ingredients_recettes_processed = pd.merge(
        df_ingredients_recettes_processed,
        df_ingredients_processed[['page_id', 'nom', 'unite', 'type_de_stock']], # Also get unit and type_de_stock
        left_on='ingredient_ok',
        right_on='page_id',
        how='left',
        suffixes=('', '_ing')
    ).rename(columns={'nom_ing': 'nom_ingredient', 'unite_ing': 'unité', 'type_de_stock_ing': 'type_de_stock'})
    df_ingredients_recettes_processed = df_ingredients_recettes_processed.drop(columns=['page_id_ing']) # Drop the redundant ID column from ingredient merge

    st.info("Génération des menus et listes d'ingrédients...")

    df_menus_complet = pd.DataFrame()
    historique_recettes_recentes = {} # {id_recette: dernière_date_utilisation}

    # Charger l'historique des menus existants
    if not df_menus_historique_notion.empty:
        df_menus_historique = clean_column_names(df_menus_historique_notion)
        # Ensure correct column names from fetch_menus_data
        df_menus_historique = df_menus_historique.rename(columns={
            'nom_menu': 'recette_nom',
            'recette': 'id_recette',
            'date': 'date'
        })
        df_menus_historique['date'] = pd.to_datetime(df_menus_historique['date'])
        for idx, row in df_menus_historique.iterrows():
            if row['id_recette']: # Can be multiple IDs from Notion relation
                for rec_id in str(row['id_recette']).split(', '):
                    rec_id = rec_id.strip()
                    if rec_id:
                        historique_recettes_recentes[rec_id] = max(
                            historique_recettes_recentes.get(rec_id, pd.Timestamp.min),
                            row['date']
                        )
        st.info(f"Historique de {len(historique_recettes_recentes)} recettes récentes chargé.")
    else:
        st.info("Aucun historique de menus trouvé ou vide.")


    for index, row_planning in df_planning.iterrows():
        date_plan = row_planning['date']
        repas_type = row_planning['repas_type']
        participants = row_planning['participant_s_']
        recette_suggeree_id = None # Stocke l'ID de la recette Notion

        # If a recipe name is already in the planning, find its ID
        if 'recette_nom' in row_planning and pd.notna(row_planning['recette_nom']) and row_planning['recette_nom'].strip():
            recette_nom_plan = row_planning['recette_nom'].strip()
            # Try to match by "Nom" from recettes
            matched_recettes = df_recettes[df_recettes['nom'].str.lower() == recette_nom_plan.lower()]
            if not matched_recettes.empty:
                recette_suggeree_id = matched_recettes.iloc[0]['page_id']
                st.write(f"Utilisation de la recette pré-définie pour le {date_plan.strftime('%d/%m/%Y')} - {repas_type}: {recette_nom_plan}")
            else:
                st.warning(f"La recette '{recette_nom_plan}' du planning n'a pas été trouvée dans la base de données Recettes. Une recette sera choisie au hasard.")
        
        # If no specific recipe or if it wasn't found, choose randomly
        if recette_suggeree_id is None:
            type_plat_desire = []
            if repas_type == 'midi':
                type_plat_desire = ['Salade', 'Plat']
            elif repas_type == 'soir':
                type_plat_desire = ['Soupe', 'Plat']

            recettes_candidates_for_type = df_recettes_exploded[
                df_recettes_exploded['type_plat'].isin(type_plat_desire)
            ]['page_id'].unique()

            # Filter out recently used recipes
            recettes_disponibles = []
            for rec_id in recettes_candidates_for_type:
                derniere_utilisation = historique_recettes_recentes.get(rec_id, pd.Timestamp.min)
                if (date_plan - derniere_utilisation).days > nb_jours_anti_repetition:
                    recettes_disponibles.append(rec_id)

            if not recettes_disponibles:
                st.warning(f"Pas de recettes disponibles non récemment utilisées pour {date_plan.strftime('%d/%m/%Y')} - {repas_type}. Réinitialisation de l'historique pour trouver une recette.")
                recettes_disponibles = list(recettes_candidates_for_type) # If no recent option, consider all

            if recettes_disponibles:
                recette_suggeree_id = random.choice(recettes_disponibles)
                st.write(f"Recette choisie au hasard pour le {date_plan.strftime('%d/%m/%Y')} - {repas_type}.")
            else:
                st.warning(f"Aucune recette candidate trouvée pour {date_plan.strftime('%d/%m/%Y')} - {repas_type}. Ce repas sera vide.")
                continue # Skip to the next meal

        # Get details of the chosen recipe
        recette_detail = df_recettes[df_recettes['page_id'] == recette_suggeree_id]
        if recette_detail.empty:
            st.error(f"Détails de la recette avec l'ID {recette_suggeree_id} introuvables. Ce repas sera ignoré.")
            continue
        
        recette_detail = recette_detail.iloc[0]
        recette_nom = recette_detail['nom']
        id_recette_notion = recette_detail['page_id'] # Notion Page ID for the recipe

        # Add to history
        historique_recettes_recentes[id_recette_notion] = date_plan

        # Prepare menu row
        nouvelle_ligne_menu = pd.DataFrame([{
            'date': date_plan,
            'repas_type': repas_type,
            'recette_nom': recette_nom,
            'id_recette': id_recette_notion,
            'Participant(s)': participants
        }])
        df_menus_complet = pd.concat([df_menus_complet, nouvelle_ligne_menu], ignore_index=True)

    st.success("Menus générés.")
    return df_menus_complet, df_ingredients_processed, df_ingredients_recettes_processed

def generate_output_files(df_menus_complet, df_ingredients_processed, df_ingredients_recettes_processed):
    st.info("Génération des fichiers de sortie...")

    # --- Génération du CSV des menus ---
    output_menu_csv = io.StringIO()
    # Select columns for export, ensuring they exist
    colonnes_export = ['date', 'repas_type', 'recette_nom', 'id_recette', 'Participant(s)']
    # The 'Participant(s)' column from input becomes 'participant_s_' after clean_column_names,
    # but we want to present it as 'Participant(s)' in the output CSV for user readability.
    # We will rename it back for the output file.
    df_menus_complet_for_export = df_menus_complet.copy()
    if 'participant_s_' in df_menus_complet_for_export.columns and 'Participant(s)' not in df_menus_complet_for_export.columns:
        df_menus_complet_for_export.rename(columns={'participant_s_': 'Participant(s)'}, inplace=True)

    actual_export_cols = [col for col in colonnes_export if col in df_menus_complet_for_export.columns]
    df_menus_complet_for_export.to_csv(output_menu_csv, index=False, encoding="utf-8-sig", columns=actual_export_cols)
    output_menu_csv.seek(0)
    st.session_state[FICHIER_SORTIE_MENU_CSV] = output_menu_csv.getvalue().encode('utf-8-sig')
    st.success(f"Fichier '{FICHIER_SORTIE_MENU_CSV}' généré.")

    # --- Génération du TXT des listes d'ingrédients ---
    contenu_fichier_recap_txt = []
    contenu_fichier_recap_txt.append(f"Récapitulatif des ingrédients pour les menus générés ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n")

    # Get recipe IDs used in generated menus
    recette_ids_in_menus = df_menus_complet['id_recette'].unique().tolist()

    # Filter df_ingredients_recettes_processed for ingredients of relevant recipes
    df_ingredients_for_menus = df_ingredients_recettes_processed[
        df_ingredients_recettes_processed['page_id_recette'].isin(recette_ids_in_menus)
    ].copy()

    # Merge with df_menus_complet to get participant count per meal
    df_merged = pd.merge(
        df_ingredients_for_menus,
        df_menus_complet[['id_recette', 'Participant(s)']], # Use original column name for merge
        left_on='page_id_recette',
        right_on='id_recette',
        how='left'
    )
    # Ensure 'Participant(s)' from merge is used correctly; it comes from df_menus_complet.
    # The 'clean_column_names' would have changed it to 'participant_s_' in df_menus_complet.
    # So we need to ensure the merge uses 'participant_s_' or 'Participant(s)' consistently.
    # In process_data, df_menus_complet has 'Participant(s)'.
    # So `df_merged['qte_totale_necessaire'] = df_merged['qte_pers_s'] * df_merged['Participant(s)']` is correct.

    df_merged['qte_totale_necessaire'] = df_merged['qte_pers_s'] * df_merged['Participant(s)']

    # Aggregate by ingredient name
    liste_courses = df_merged.groupby('nom_ingredient').agg(
        total_qte=('qte_totale_necessaire', 'sum'),
        unite=('unité', lambda x: x.mode()[0] if not x.mode().empty else ''), # Get most frequent unit
        type_de_stock=('type_de_stock', lambda x: x.mode()[0] if not x.mode().empty else '') # Get most frequent stock type
    ).reset_index()

    # Join with df_ingredients_processed for remaining quantity in stock
    liste_courses = pd.merge(
        liste_courses,
        df_ingredients_processed[['nom', 'qte_reste']],
        left_on='nom_ingredient',
        right_on='nom',
        how='left',
        suffixes=('', '_stock')
    )
    liste_courses['qte_reste'] = pd.to_numeric(liste_courses['qte_reste'], errors='coerce').fillna(0)
    liste_courses['qte_a_acheter'] = liste_courses['total_qte'] - liste_courses['qte_reste']
    liste_courses = liste_courses[liste_courses['qte_a_acheter'] > 0] # Display only what needs to be bought

    if not liste_courses.empty:
        contenu_fichier_recap_txt.append("--- Liste de Courses ---\n")
        for idx, row in liste_courses.iterrows():
            qte = f"{row['qte_a_acheter']:.2f}".replace('.', ',')
            contenu_fichier_recap_txt.append(f"- {row['nom_ingredient']}: {qte} {row['unite']} ({row['type_de_stock']})\n")
    else:
        contenu_fichier_recap_txt.append("Aucun ingrédient à acheter pour les menus générés.\n")

    st.session_state[FICHIER_SORTIE_LISTES_TXT] = "".join(contenu_fichier_recap_txt).encode('utf-8')
    st.success(f"Fichier '{FICHIER_SORTIE_LISTES_TXT}' généré.")


def main():
    st.set_page_config(layout="wide", page_title="Générateur de Menus Automatisé")
    st.title("🍽️ Générateur de Menus et Listes")

    st.sidebar.header("Configuration & Chargement")

    data_source_option = st.sidebar.radio(
        "Source des données Notion :",
        ("Charger depuis Notion (recommandé)", "Charger depuis des fichiers CSV")
    )

    # Initialize dataframes in session state to persist them across reruns
    if 'df_planning' not in st.session_state: st.session_state['df_planning'] = pd.DataFrame()
    if 'df_recettes' not in st.session_state: st.session_state['df_recettes'] = pd.DataFrame()
    if 'df_ingredients' not in st.session_state: st.session_state['df_ingredients'] = pd.DataFrame()
    if 'df_ingredients_recettes' not in st.session_state: st.session_state['df_ingredients_recettes'] = pd.DataFrame()
    if 'df_menus_historique_notion' not in st.session_state: st.session_state['df_menus_historique_notion'] = pd.DataFrame()

    if data_source_option == "Charger depuis Notion (recommandé)":
        st.sidebar.info("Les données seront extraites directement de vos bases de données Notion.")

        saison_filtre = st.sidebar.selectbox(
            "Filtrer les recettes par saison (laissez vide pour ignorer):",
            ["", "Printemps", "Été", "Automne", "Hiver", "Toute l'année"],
            index=1 # Default to Printemps as in original code
        )

        if st.sidebar.button("Extraire les données de Notion"):
            with st.spinner("Extraction des données Notion..."):
                st.session_state['df_ingredients'] = fetch_ingredients_data()
                st.session_state['df_ingredients_recettes'] = fetch_ingredients_recettes_data()
                st.session_state['df_recettes'] = fetch_recettes_data(saison_filtre)
                st.session_state['df_menus_historique_notion'] = fetch_menus_data()

                if st.session_state['df_ingredients'].empty or st.session_state['df_ingredients_recettes'].empty or st.session_state['df_recettes'].empty:
                    st.error("Échec de l'extraction d'une ou plusieurs bases de données Notion. Veuillez vérifier les logs et les secrets.")
                else:
                    st.sidebar.success("Données Notion extraites avec succès dans la session.")
    else:
        st.sidebar.info("Veuillez charger vos fichiers CSV manuellement.")

    st.sidebar.subheader("Charger le fichier Planning (obligatoire)")
    uploaded_planning_file = st.sidebar.file_uploader("Choisissez Planning.csv", type="csv", key="planning_uploader")
    if uploaded_planning_file is not None:
        try:
            df_planning_temp = pd.read_csv(uploaded_planning_file)
            st.session_state['df_planning'] = df_planning_temp
            st.sidebar.success("Fichier Planning.csv chargé avec succès.")
            st.sidebar.dataframe(df_planning_temp.head(2))
        except Exception as e:
            st.sidebar.error(f"Erreur lors du chargement de Planning.csv: {e}")

    if data_source_option == "Charger depuis des fichiers CSV":
        st.sidebar.subheader("Charger les autres fichiers CSV")
        uploaded_ingredients_file = st.sidebar.file_uploader("Choisissez Ingredients.csv", type="csv", key="ingredients_uploader")
        uploaded_ingredients_recettes_file = st.sidebar.file_uploader("Choisissez Ingredients_recettes.csv", type="csv", key="ingredients_recettes_uploader")
        uploaded_recettes_file = st.sidebar.file_uploader("Choisissez Recettes.csv", type="csv", key="recettes_uploader")
        uploaded_menus_file = st.sidebar.file_uploader("Choisissez Menus.csv (pour historique)", type="csv", key="menus_uploader")

        if uploaded_ingredients_file:
            try:
                st.session_state['df_ingredients'] = pd.read_csv(uploaded_ingredients_file)
                st.sidebar.success("Fichier Ingredients.csv chargé.")
            except Exception as e: st.sidebar.error(f"Erreur chargement Ingredients.csv: {e}")
        if uploaded_ingredients_recettes_file:
            try:
                st.session_state['df_ingredients_recettes'] = pd.read_csv(uploaded_ingredients_recettes_file)
                st.sidebar.success("Fichier Ingredients_recettes.csv chargé.")
            except Exception as e: st.sidebar.error(f"Erreur chargement Ingredients_recettes.csv: {e}")
        if uploaded_recettes_file:
            try:
                st.session_state['df_recettes'] = pd.read_csv(uploaded_recettes_file)
                st.sidebar.success("Fichier Recettes.csv chargé.")
            except Exception as e: st.sidebar.error(f"Erreur chargement Recettes.csv: {e}")
        if uploaded_menus_file:
            try:
                st.session_state['df_menus_historique_notion'] = pd.read_csv(uploaded_menus_file)
                st.sidebar.success("Fichier Menus.csv chargé pour l'historique.")
            except Exception as e: st.sidebar.error(f"Erreur chargement Menus.csv: {e}")


    # Main application logic
    st.header("1. Pré-requis et Aperçu des Données")
    st.write("Assurez-vous que tous les fichiers nécessaires sont chargés ou que les données Notion ont été extraites.")

    # Display status of loaded data
    st.subheader("Statut des données :")
    data_present = {}
    data_present['Planning'] = not st.session_state['df_planning'].empty
    data_present['Ingrédients'] = not st.session_state['df_ingredients'].empty
    data_present['Ingrédients Recettes'] = not st.session_state['df_ingredients_recettes'].empty
    data_present['Recettes'] = not st.session_state['df_recettes'].empty
    data_present['Menus Historique'] = not st.session_state['df_menus_historique_notion'].empty

    for name, is_present in data_present.items():
        if is_present:
            st.success(f"✅ Données '{name}' chargées. ({len(st.session_state.get('df_' + name.lower().replace(' ', '_').replace('ingrédients', 'ingredients'), pd.DataFrame()))} lignes)")
        else:
            st.warning(f"❌ Données '{name}' non chargées ou vides.")

    # Define the mandatory datasets for processing
    mandatory_datasets = ['Planning', 'Ingrédients', 'Ingrédients Recettes', 'Recettes']
    all_mandatory_data_loaded = all(data_present[key] for key in mandatory_datasets)


    if all_mandatory_data_loaded:
        st.header("2. Générer les menus et listes")
        if st.button("Générer les Menus et Listes"):
            with st.spinner("Génération en cours..."):
                try:
                    df_menus_complet, df_ingredients_processed, df_ingredients_recettes_processed = process_data(
                        st.session_state['df_planning'],
                        st.session_state['df_recettes'],
                        st.session_state['df_ingredients'],
                        st.session_state['df_ingredients_recettes'],
                        st.session_state['df_menus_historique_notion'] # Pass historical menus
                    )

                    if not df_menus_complet.empty:
                        st.subheader("Aperçu des Menus Générés :")
                        st.dataframe(df_menus_complet[['date', 'repas_type', 'recette_nom', 'Participant(s)']])

                        generate_output_files(df_menus_complet, df_ingredients_processed, df_ingredients_recettes_processed)

                        st.header("3. Télécharger les fichiers générés")
                        if FICHIER_SORTIE_MENU_CSV in st.session_state:
                            st.download_button(
                                label=f"Télécharger {FICHIER_SORTIE_MENU_CSV}",
                                data=st.session_state[FICHIER_SORTIE_MENU_CSV],
                                file_name=FICHIER_SORTIE_MENU_CSV,
                                mime="text/csv",
                                key="download_menu_csv"
                            )
                        if FICHIER_SORTIE_LISTES_TXT in st.session_state:
                            st.download_button(
                                label=f"Télécharger {FICHIER_SORTIE_LISTES_TXT}",
                                data=st.session_state[FICHIER_SORTIE_LISTES_TXT],
                                file_name=FICHIER_SORTIE_LISTES_TXT,
                                mime="text/plain",
                                key="download_lists_txt"
                            )
                        st.success("Processus de génération terminé. Les fichiers sont prêts à être téléchargés.")
                    else:
                        st.warning("Aucun menu n'a pu être généré. Veuillez vérifier vos fichiers d'entrée et les conditions de filtre.")
                except Exception as e:
                    st.error(f"Une erreur est survenue pendant la génération : {e}")
                    logger.exception("Erreur pendant la génération des menus.")
    else:
        st.warning("Veuillez charger tous les fichiers CSV ou extraire toutes les données Notion nécessaires pour activer la génération. (Planning, Ingrédients, Ingrédients Recettes, Recettes sont obligatoires).")

    st.info("N'oubliez pas de configurer vos secrets Notion dans `.streamlit/secrets.toml` si vous utilisez l'option 'Charger depuis Notion'.")
    st.markdown("""
    Exemple de `.streamlit/secrets.toml`:
    ```toml
    notion_api_key = "secret_YOUR_NOTION_API_KEY"
    notion_database_id_ingredients = "b23b048b67334032ac1ae4e82d308817" # Default from your provided code
    notion_database_id_ingredients_recettes = "1d16fa46f8b2805b8377eba7bf668eb5" # Default from your provided code
    notion_database_id_recettes = "1d16fa46f8b2805b8377eba7bf668eb5" # Default from your provided code
    notion_database_id_menus = "9025cfa1c18d4501a91dbeb1b10b48bd" # Default from your provided code
    ```
    """)


if __name__ == "__main__":
    main()
