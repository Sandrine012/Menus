import pandas as pd
import requests
import json
import random
import streamlit as st
import os

# ─── Clés d'authentification Notion ─────────────────────────────
# IMPORTANT : Remplacez ces valeurs par vos propres clés et identifiants
notion_api_key="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"
 
# ─── IDs des bases de données Notion ────────────────────────────
# Seule la base de données des ingrédients est utilisée depuis Notion
ID_INGREDIENTS = "b23b048b67334032ac1ae4e82d308817"

# ─── Variables globales pour l'API Notion ───────────────────────
API_URL = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {notion_api_key}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

# ─── Constantes pour les colonnes des DataFrames ────────────────
COLONNE_ID_RECETTE = "page_id"
COLONNE_ID_INGREDIENT = "page_id"
COLONNE_NOM_RECETTE = "Nom"
COLONNE_NOM_INGREDIENT = "Nom"
COLONNE_POURCENTAGE_STOCK = "Pourcentage_stock"
COLONNE_SCORE_TOTAL = "Score_total"
COLONNE_SCORE_FREQUENCE = "Score_frequence"
COLONNE_SCORE_STOCK = "Score_stock"

# ─── Fonctions d'aide pour l'API Notion ─────────────────────────
def paginate(database_id, filter_prop=None):
    start_cursor = None
    while True:
        url = f"{API_URL}/databases/{database_id}/query"
        data = {"filter": filter_prop} if filter_prop else {}
        if start_cursor:
            data["start_cursor"] = start_cursor
        response = requests.post(url, headers=HEADERS, data=json.dumps(data))
        if response.status_code != 200:
            st.error(f"Erreur lors de la requête Notion : {response.text}")
            return
        response_json = response.json()
        yield from response_json["results"]
        start_cursor = response_json.get("next_cursor")
        if not start_cursor:
            break

# ─── Fonctions pour extraire les données depuis Notion et CSV ───
HDR_INGREDIENTS = [COLONNE_ID_INGREDIENT, "Nom", "unité", "Qte reste"]
def extract_ingredients_from_notion():
    rows = []
    try:
        for p in paginate(ID_INGREDIENTS):
            pr = p["properties"]
            page_id = p["id"]
            nom = "".join(t["plain_text"] for t in pr["Nom"]["title"])
            unite = pr["unité"]["select"]["name"] if pr["unité"]["select"] else ""
            qte_reste = pr["Qte reste"]["number"] if pr["Qte reste"] and "number" in pr["Qte reste"] and pr["Qte reste"]["number"] is not None else 0
            rows.append([page_id, nom.strip(), unite.strip(), qte_reste])
    except Exception as e:
        st.error(f"Erreur lors de la récupération des ingrédients depuis Notion : {e}")
        return pd.DataFrame(columns=HDR_INGREDIENTS)
    return pd.DataFrame(rows, columns=HDR_INGREDIENTS)

def load_recipes_from_csv(file_path="Recettes.csv"):
    try:
        df = pd.read_csv(file_path)
        return df
    except FileNotFoundError:
        st.error(f"Fichier non trouvé : {file_path}. Veuillez vous assurer que le fichier existe.")
        return pd.DataFrame()
    except Exception as e:
        st.error(f"Erreur lors du chargement de {file_path} : {e}")
        return pd.DataFrame()

def load_ingredients_recipes_from_csv(file_path="Ingredients_recettes.csv"):
    try:
        df = pd.read_csv(file_path)
        return df
    except FileNotFoundError:
        st.error(f"Fichier non trouvé : {file_path}. Veuillez vous assurer que le fichier existe.")
        return pd.DataFrame()
    except Exception as e:
        st.error(f"Erreur lors du chargement de {file_path} : {e}")
        return pd.DataFrame()

def load_all_data():
    st.info("Chargement des ingrédients depuis Notion et des recettes depuis les fichiers CSV...")
    st.session_state['df_ingredients'] = extract_ingredients_from_notion()
    st.session_state['df_recipes'] = load_recipes_from_csv()
    st.session_state['df_ingredients_recipes'] = load_ingredients_recipes_from_csv()
    
    if not st.session_state['df_ingredients'].empty and not st.session_state['df_recipes'].empty and not st.session_state['df_ingredients_recipes'].empty:
        st.success("Données chargées avec succès.")
        st.write("Aperçu de la base d'ingrédients (Notion) :")
        st.dataframe(st.session_state['df_ingredients'].head())
        st.write("Aperçu des recettes (CSV) :")
        st.dataframe(st.session_state['df_recipes'].head())
    else:
        st.error("Erreur de chargement : Un ou plusieurs DataFrames sont vides. Veuillez vérifier les fichiers CSV et la connexion à Notion.")

# ─── Fonctions de calcul ────────────────────────────────────────
def calculate_stock_score(df_recipes, df_ingredients, df_ingredients_recipes):
    stock_scores = {}
    for index, recipe in df_recipes.iterrows():
        recipe_id = recipe[COLONNE_ID_RECETTE]
        ingredients_in_recipe = df_ingredients_recipes[df_ingredients_recipes["Recette"] == recipe_id]["Ingredient"]
        
        ingredients_count = len(ingredients_in_recipe)
        if ingredients_count == 0:
            stock_scores[recipe_id] = 0
            continue
            
        stock_match_count = 0
        for ingredient_id in ingredients_in_recipe:
            ingredient = df_ingredients[df_ingredients[COLONNE_ID_INGREDIENT] == ingredient_id]
            if not ingredient.empty and ingredient["Qte reste"].iloc[0] > 0:
                stock_match_count += 1
        
        stock_scores[recipe_id] = (stock_match_count / ingredients_count) * 100
        
    df_recipes[COLONNE_POURCENTAGE_STOCK] = df_recipes[COLONNE_ID_RECETTE].map(stock_scores)
    return df_recipes

def generate_menu(
    num_recettes=7,
    poids_frequence=0.5,
    poids_stock=0.5
):
    df_recipes = st.session_state['df_recipes'].copy()
    df_ingredients = st.session_state['df_ingredients'].copy()
    df_ingredients_recipes = st.session_state['df_ingredients_recipes'].copy()
    
    df_recipes = calculate_stock_score(df_recipes, df_ingredients, df_ingredients_recipes)
    
    # Normalisation des scores
    max_frequence = df_recipes["Frequence"].max()
    min_frequence = df_recipes["Frequence"].min()
    if max_frequence != min_frequence:
        df_recipes[COLONNE_SCORE_FREQUENCE] = (df_recipes["Frequence"] - min_frequence) / (max_frequence - min_frequence)
    else:
        df_recipes[COLONNE_SCORE_FREQUENCE] = 0
        
    max_stock = df_recipes[COLONNE_POURCENTAGE_STOCK].max()
    min_stock = df_recipes[COLONNE_POURCENTAGE_STOCK].min()
    if max_stock != min_stock:
        df_recipes[COLONNE_SCORE_STOCK] = (df_recipes[COLONNE_POURCENTAGE_STOCK] - min_stock) / (max_stock - min_stock)
    else:
        df_recipes[COLONNE_SCORE_STOCK] = 0
        
    # Calcul du score total
    df_recipes[COLONNE_SCORE_TOTAL] = (poids_frequence * df_recipes[COLONNE_SCORE_FREQUENCE]) + (poids_stock * df_recipes[COLONNE_SCORE_STOCK])
    
    # Sélection des recettes
    df_recipes_triees = df_recipes.sort_values(by=COLONNE_SCORE_TOTAL, ascending=False)
    
    # Sélection des 7 recettes les plus pertinentes
    selected_recipes = df_recipes_triees.head(num_recettes)
    
    # Choix aléatoire pour diversifier
    if len(df_recipes_triees) > num_recettes:
        weights = df_recipes_triees[COLONNE_SCORE_TOTAL] / df_recipes_triees[COLONNE_SCORE_TOTAL].sum()
        selected_recipes = df_recipes_triees.sample(n=num_recettes, weights=weights, replace=False)
    else:
        selected_recipes = df_recipes_triees
        
    return selected_recipes

# ─── Logique principale pour l'application Streamlit ───────────
st.title("Générateur de menus")

if 'df_ingredients' not in st.session_state:
    st.session_state['df_ingredients'] = None
if 'df_recipes' not in st.session_state:
    st.session_state['df_recipes'] = None
if 'df_ingredients_recipes' not in st.session_state:
    st.session_state['df_ingredients_recipes'] = None

if st.button("1. Charger les données"):
    load_all_data()

if st.button("2. Générer le menu"):
    if st.session_state['df_ingredients'] is None:
        st.warning("Veuillez d'abord charger les données.")
    else:
        st.info("Génération du menu...")
        menu = generate_menu()
        if not menu.empty:
            st.write("### Menu généré avec succès")
            st.dataframe(menu[[COLONNE_NOM_RECETTE, COLONNE_POURCENTAGE_STOCK]])
        else:
            st.error("Aucune recette disponible pour la génération du menu.")

if st.button("3. Réinitialiser les variables"):
    st.session_state['df_ingredients'] = None
    st.session_state['df_recipes'] = None
    st.session_state['df_ingredients_recipes'] = None
    st.success("Variables réinitialisées. Veuillez recharger les données pour continuer.")
