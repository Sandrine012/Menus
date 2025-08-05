import pandas as pd
import requests
import json
import random
from IPython.display import display
from ipywidgets import Button, VBox, Output

# ─── Clés d'authentification Notion ─────────────────────────────
# IMPORTANT : Remplacez ces valeurs par vos propres clés et identifiants
notion_api_key="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"
# ─── IDs des bases de données Notion ────────────────────────────
ID_INGREDIENTS = "b23b048b67334032ac1ae4e82d308817"
ID_INGREDIENTS_RECETTES = "1d16fa46f8b2805b8377eba7bf668eb5"
ID_RECETTES = "1d16fa46f8b2805b8377eba7bf668eb5"
ID_MENUS = "9025cfa1c18d4501a91dbeb1b10b48bd"

# ─── Variables globales ─────────────────────────────────────────
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
def query_database(database_id, filter_prop=None):
    url = f"{API_URL}/databases/{database_id}/query"
    data = {"filter": filter_prop} if filter_prop else {}
    return requests.post(url, headers=HEADERS, data=json.dumps(data))

def get_page(page_id):
    url = f"{API_URL}/pages/{page_id}"
    return requests.get(url, headers=HEADERS)

def paginate(database_id, filter_prop=None):
    start_cursor = None
    while True:
        url = f"{API_URL}/databases/{database_id}/query"
        data = {"filter": filter_prop} if filter_prop else {}
        if start_cursor:
            data["start_cursor"] = start_cursor

        response = requests.post(url, headers=HEADERS, data=json.dumps(data))
        if response.status_code != 200:
            print(f"Erreur lors de la requête Notion : {response.text}")
            return
        
        response_json = response.json()
        yield from response_json["results"]
        start_cursor = response_json.get("next_cursor")
        if not start_cursor:
            break

# NOUVEAU : Fonction pour extraire les données des ingrédients depuis Notion
HDR_INGREDIENTS = [COLONNE_ID_INGREDIENT, "Nom", "unité", "Qte reste"]
def extract_ingredients():
    rows = []
    try:
        for p in paginate(ID_INGREDIENTS):
            pr = p["properties"]
            page_id = p["id"]
            nom = "".join(t["plain_text"] for t in pr["Nom"]["title"])
            unite = pr["unité"]["select"]["name"] if pr["unité"]["select"] else ""
            # Ligne corrigée pour gérer les valeurs vides de la colonne "Qte reste"
            qte_reste = pr["Qte reste"]["number"] if pr["Qte reste"] and "number" in pr["Qte reste"] and pr["Qte reste"]["number"] is not None else 0
            
            rows.append([page_id, nom.strip(), unite.strip(), qte_reste])
    except Exception as e:
        print(f"Erreur lors de la récupération des ingrédients depuis Notion : {e}")
        return pd.DataFrame(columns=HDR_INGREDIENTS)
    return pd.DataFrame(rows, columns=HDR_INGREDIENTS)


def extract_recipes():
    HDR_RECETTES = [COLONNE_ID_RECETTE, "Nom", "Frequence", "Url"]
    rows = []
    try:
        for p in paginate(ID_RECETTES):
            pr = p["properties"]
            page_id = p["id"]
            nom = "".join(t["plain_text"] for t in pr["Nom"]["title"])
            frequence = pr["Frequence"]["number"] if pr["Frequence"]["number"] else 0
            url = pr["Url"]["url"] if pr["Url"]["url"] else ""
            rows.append([page_id, nom.strip(), frequence, url.strip()])
    except Exception as e:
        print(f"Erreur lors de la récupération des recettes depuis Notion : {e}")
        return pd.DataFrame(columns=HDR_RECETTES)
    return pd.DataFrame(rows, columns=HDR_RECETTES)

def extract_ingredients_recettes():
    HDR_INGREDIENTS_RECETTES = ["Recette", "Ingredient"]
    rows = []
    try:
        for p in paginate(ID_INGREDIENTS_RECETTES):
            pr = p["properties"]
            page_id = p["id"]
            if pr["Recette"]["relation"] and pr["Ingredient"]["relation"]:
                recette_id = pr["Recette"]["relation"][0]["id"]
                ingredient_id = pr["Ingredient"]["relation"][0]["id"]
                rows.append([recette_id, ingredient_id])
    except Exception as e:
        print(f"Erreur lors de la récupération des ingrédients des recettes depuis Notion : {e}")
        return pd.DataFrame(columns=HDR_INGREDIENTS_RECETTES)
    return pd.DataFrame(rows, columns=HDR_INGREDIENTS_RECETTES)

def load_all_data():
    global df_ingredients, df_recipes, df_ingredients_recipes
    print("Chargement des données depuis Notion...")
    df_ingredients = extract_ingredients()
    df_recipes = extract_recipes()
    df_ingredients_recipes = extract_ingredients_recettes()
    if not df_ingredients.empty and not df_recipes.empty and not df_ingredients_recipes.empty:
        print("Données chargées avec succès.")
    else:
        print("Erreur de chargement : Un ou plusieurs DataFrames sont vides.")
        
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
    df_recipes,
    df_ingredients,
    df_ingredients_recipes,
    num_recettes=7,
    poids_frequence=0.5,
    poids_stock=0.5
):
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
        # Augmenter la pondération des recettes bien notées pour le choix aléatoire
        weights = df_recipes_triees[COLONNE_SCORE_TOTAL] / df_recipes_triees[COLONNE_SCORE_TOTAL].sum()
        selected_recipes = df_recipes_triees.sample(n=num_recettes, weights=weights, replace=False)
    else:
        selected_recipes = df_recipes_triees
        
    return selected_recipes

# ─── Widgets interactifs pour l'interface ───────────────────────
df_ingredients, df_recipes, df_ingredients_recipes = None, None, None

def on_load_data_button_clicked(b):
    with output:
        output.clear_output()
        load_all_data()
        
def on_generate_menu_button_clicked(b):
    with output:
        output.clear_output()
        if df_ingredients is None or df_recipes is None or df_ingredients_recipes is None:
            print("Veuillez d'abord charger les données.")
            return
            
        print("Génération du menu...")
        menu = generate_menu(df_recipes.copy(), df_ingredients.copy(), df_ingredients_recipes.copy())
        if not menu.empty:
            display(menu[[COLONNE_NOM_RECETTE, COLONNE_POURCENTAGE_STOCK]])
            print("Menu généré avec succès.")
        else:
            print("Aucune recette disponible pour la génération du menu.")

def on_reset_variables_button_clicked(b):
    global df_ingredients, df_recipes, df_ingredients_recipes
    with output:
        output.clear_output()
        df_ingredients, df_recipes, df_ingredients_recipes = None, None, None
        print("Variables réinitialisées. Veuillez recharger les données pour continuer.")

load_data_button = Button(description="1. Charger les données")
generate_menu_button = Button(description="2. Générer le menu")
reset_variables_button = Button(description="3. Réinitialiser")

load_data_button.on_click(on_load_data_button_clicked)
generate_menu_button.on_click(on_generate_menu_button_clicked)
reset_variables_button.on_click(on_reset_variables_button_clicked)

output = Output()

display(VBox([load_data_button, generate_menu_button, reset_variables_button, output]))
