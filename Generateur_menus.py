import streamlit as st
import pandas as pd
import random
import logging
from datetime import datetime, timedelta
import time, httpx
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError

# ────── CONFIGURATION INITIALE ──────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')
logger = logging.getLogger(__name__)

# Constantes globales
NB_JOURS_ANTI_REPETITION = 42
COLONNE_NOM = "Nom"
COLONNE_TEMPS_TOTAL = "Temps_total"
COLONNE_ID_RECETTE = "Page_ID"
COLONNE_ID_INGREDIENT = "Page_ID"
COLONNE_AIME_PAS_PRINCIP = "Aime_pas_princip"
VALEUR_DEFAUT_TEMPS_PREPARATION = 10
TEMPS_MAX_EXPRESS = 20
TEMPS_MAX_RAPIDE = 30
REPAS_EQUILIBRE = 700

# ────── AJOUT DES DÉPENDANCES NOTION ───────────────────────────
NOTION_API_KEY = st.secrets["notion_api_key"]
ID_RECETTES = st.secrets["notion_database_id_recettes"]
ID_MENUS = st.secrets["notion_database_id_menus"]
ID_INGREDIENTS = st.secrets["notion_database_id_ingredients"]
ID_INGREDIENTS_RECETTES = st.secrets["notion_database_id_ingredients_recettes"]
BATCH_SIZE, MAX_RETRY, WAIT_S = 50, 3, 5
notion = Client(auth=NOTION_API_KEY)

# ────── AJOUT DES FONCTIONS D'EXTRACTION NOTION ─────────────────
def paginate(db_id, **kwargs):
    out, cur, retry = [], None, 0
    while True:
        try:
            resp = notion.databases.query(database_id=db_id,
                                          start_cursor=cur,
                                          page_size=BATCH_SIZE,
                                          **kwargs)
            out.extend(resp["results"])
            if not resp["has_more"]:
                break
            cur = resp["next_cursor"]
            time.sleep(0.3)
            retry = 0
        except (RequestTimeoutError, httpx.TimeoutException, httpx.ReadTimeout):
            retry += 1
            if retry > MAX_RETRY:
                st.error("Timeout répété – arrêt.")
                break
            time.sleep(WAIT_S * retry)
        except APIResponseError as e:
            st.error(f"Erreur API : {e}")
            break
    return out

HDR_RECETTES = ["Page_ID","Nom","ID_Recette","Saison",
                "Calories","Proteines","Temps_total",
                "Aime_pas_princip","Type_plat","Transportable"]
MAP_REC = {
    "Nom":("Nom_plat","title"), "ID_Recette":("ID_Recette","uid"),
    "Saison":("Saison","ms"),   "Calories":("Calories Recette","roll"),
    "Proteines":("Proteines Recette","roll"),
    "Temps_total":("Temps_total","form"), "Aime_pas_princip":("Aime_pas_princip","rollstr"),
    "Type_plat":("Type_plat","ms"), "Transportable":("Transportable","selcb")
}
SAISON_FILTRE = "Printemps"
def prop_val(p,k):
    if not p: return ""
    t = p["type"]
    if k=="title":   return "".join(x["plain_text"] for x in p["title"])
    if k=="uid":     u=p["unique_id"]; pr,nu=u.get("prefix"),u.get("number"); return f"{pr}-{nu}" if pr else str(nu or "")
    if k=="ms":      return ", ".join(o["name"] for o in p["multi_select"])
    if k=="roll":    return str(p["rollup"].get("number") or "")
    if k=="form":    fo=p["formula"]; return str(fo.get("number") or fo.get("string") or "")
    if k=="rollstr": return ", ".join(it["formula"].get("string") or "." for it in p["rollup"]["array"])
    if k=="selcb":   return "Oui" if (t=="select" and (p["select"] or {}).get("name","").lower()=="oui") or (t=="checkbox" and p["checkbox"]) else ""
    return ""
def extract_recettes():
    filt = {"and":[
        {"property":"Elément parent","relation":{"is_empty":True}},
        {"or":[
            {"property":"Saison","multi_select":{"contains":"Toute l'année"}},
            {"property":"Saison","multi_select":{"contains":SAISON_FILTRE}},
            {"property":"Saison","multi_select":{"is_empty":True}}]},
        {"or":[
            {"property":"Type_plat","multi_select":{"contains":"Salade"}},
            {"property":"Type_plat","multi_select":{"contains":"Soupe"}},
            {"property":"Type_plat","multi_select":{"contains":"Plat"}}]}]}
    rows=[]
    for p in paginate(ID_RECETTES, filter=filt):
        pr=p["properties"]; row=[p["id"]]
        for col in HDR_RECETTES[1:]:
            key,kind=MAP_REC[col]; row.append(prop_val(pr.get(key),kind))
        rows.append(row)
    return pd.DataFrame(rows,columns=HDR_RECETTES)

HDR_MENUS = ["Nom Menu","Recette","Date"]
def extract_menus():
    rows=[]
    for p in paginate(ID_MENUS,
            filter={"property":"Recette","relation":{"is_not_empty":True}}):
        pr = p["properties"]
        nom = "".join(t["plain_text"] for t in pr["Nom Menu"]["title"])
        rec_ids=[]
        rel=pr["Recette"]
        if rel["type"]=="relation":
            rec_ids=[r["id"] for r in rel["relation"]]
        else:
            for it in rel["rollup"]["array"]:
                rec_ids.extend([it.get("id")] if it.get("id") else
                               [r["id"] for r in it.get("relation",[])])
        d=""
        if pr["Date"]["date"] and pr["Date"]["date"]["start"]:
            d=datetime.fromisoformat(pr["Date"]["date"]["start"].replace("Z","+00:00")).strftime("%Y-%m-%d")
        rows.append([nom.strip(), ", ".join(rec_ids), d])
    return pd.DataFrame(rows,columns=HDR_MENUS)

# En-tête mis à jour pour la liste des ingrédients
HDR_INGR = ["Page_ID","Nom","Type de stock","unité","Qte reste"]
def extract_ingredients():
    rows=[]
    for p in paginate(ID_INGREDIENTS):
        pr=p["properties"]

        # 1. Extraction de l'unité
        u_prop = pr.get("unité",{})
        if u_prop.get("type")=="rich_text":
            unite="".join(t["plain_text"] for t in u_prop["rich_text"])
        elif u_prop.get("type")=="select":
            unite=(u_prop["select"] or {}).get("name","")
        else:
            unite=""

        # 2. Extraction de la quantité en stock
        qte_stock_prop = pr.get("Qté stock", {})
        qte_stock = qte_stock_prop.get("number", 0) if qte_stock_prop.get("type") == "number" else 0

        # 3. Extraction de la quantité utilisée dans les menus (agrégation/rollup)
        qte_menus_prop = pr.get("Qte Menus", {})
        qte_menus = 0
        if qte_menus_prop.get("type") == "rollup":
            rollup_result = qte_menus_prop.get("rollup", {})
            if rollup_result.get("type") == "number":
                qte_menus = rollup_result.get("number", 0)

        # 4. Reconstitution de la formule pour Qté reste
        qte_reste = max(0, qte_stock - qte_menus)

        rows.append([
            p["id"],
            "".join(t["plain_text"] for t in pr["Nom"]["title"]),
            (pr["Type de stock"]["select"] or {}).get("name",""),
            unite,
            qte_reste
        ])
    return pd.DataFrame(rows,columns=HDR_INGR)


HDR_IR = ["Page_ID","Qté/pers_s","Ingrédient ok","Type de stock f"]
def extract_ingr_rec():
    rows=[]
    for p in paginate(ID_INGREDIENTS_RECETTES,
            filter={"property":"Type de stock f","formula":{"string":{"equals":"Autre type"}}}):
        pr=p["properties"]
        parent = pr.get("Elément parent",{})
        pid = ""
        if parent and parent["type"]=="relation" and parent["relation"]:
            pid = parent["relation"][0]["id"]
        if not pid:
            pid = p["id"]
        qte = pr["Qté/pers_s"]["number"]
        if qte and qte>0:
            rows.append([
                pid,
                str(qte),
                ", ".join(r["id"] for r in pr["Ingrédient ok"]["relation"]),
                pr["Type de stock f"]["formula"]["string"] or ""
            ])
    return pd.DataFrame(rows,columns=HDR_IR)

# ────── FIN DES FONCTIONS D'EXTRACTION ───────────────────────────

def verifier_colonnes(df, colonnes_attendues, nom_fichier=""):
    """Vérifie si toutes les colonnes attendues sont présentes dans le DataFrame."""
    colonnes_manquantes = [col for col in colonnes_attendues if col not in df.columns]
    if colonnes_manquantes:
        st.error(f"Colonnes manquantes dans {nom_fichier}: {', '.join(colonnes_manquantes)}")
        raise ValueError(f"Colonnes manquantes dans {nom_fichier}: {colonnes_manquantes}")

class RecetteManager:
    """Gère l'accès et les opérations sur les données de recettes et ingrédients."""
    def __init__(self, df_recettes, df_ingredients, df_ingredients_recettes):
        self.df_recettes = df_recettes.copy()
        if COLONNE_ID_RECETTE in self.df_recettes.columns and not self.df_recettes.index.name == COLONNE_ID_RECETTE:
            self.df_recettes = self.df_recettes.set_index(COLONNE_ID_RECETTE, drop=False)

        self.df_ingredients_initial = df_ingredients.copy()
        self.df_ingredients_recettes = df_ingredients_recettes.copy()

        self.stock_simule = self.df_ingredients_initial.copy()
        if "Qte reste" in self.stock_simule.columns:
            self.stock_simule["Qte reste"] = pd.to_numeric(self.stock_simule["Qte reste"], errors='coerce').fillna(0).astype(float)
        else:
            logger.error("'Qte reste' manquante dans df_ingredients pour stock_simule.")
            self.stock_simule["Qte reste"] = 0.0

        self.anti_gaspi_ingredients = self._trouver_ingredients_stock_eleve()

    def get_ingredients_for_recipe(self, recette_id_str):
        try:
            recette_id_str = str(recette_id_str)
            ingredients = self.df_ingredients_recettes[
                self.df_ingredients_recettes[COLONNE_ID_RECETTE].astype(str) == recette_id_str
            ][["Ingrédient ok", "Qté/pers_s"]].to_dict('records')
            return ingredients
        except Exception as e:
            logger.error(f"Erreur récupération ingrédients pour {recette_id_str} : {e}")
            return []

    def _trouver_ingredients_stock_eleve(self):
        seuil_gr = 100
        seuil_pc = 1
        ingredients_stock = {}
        if not all(col in self.stock_simule.columns for col in ["Qte reste", "unité", COLONNE_ID_INGREDIENT, "Nom"]):
            logger.warning("Colonnes manquantes dans stock_simule pour _trouver_ingredients_stock_eleve.")
            return {}

        for _, row in self.stock_simule.iterrows():
            try:
                qte = float(str(row["Qte reste"]).replace(",", "."))
                unite = str(row["unité"]).lower()
                page_id = str(row[COLONNE_ID_INGREDIENT])
                if (unite in ["gr", "g", "ml", "cl"] and qte >= seuil_gr) or \
                   (unite in ["pc", "tranches"] and qte >= seuil_pc):
                    ingredients_stock[page_id] = row["Nom"]
            except (ValueError, KeyError) as e:
                logger.debug(f"Erreur dans _trouver_ingredients_stock_eleve pour ligne {row.get('Nom', 'ID inconnu')}: {e}")
                continue
        return ingredients_stock

    def recette_utilise_ingredient_anti_gaspi(self, recette_id_str):
        try:
            ingredients = self.get_ingredients_for_recipe(recette_id_str)
            return any(str(ing.get("Ingrédient ok")) in self.anti_gaspi_ingredients for ing in ingredients if ing.get("Ingrédient ok"))
        except Exception as e:
            logger.error(f"Erreur dans recette_utilise_ingredient_anti_gaspi pour {recette_id_str} : {e}")
            return False

    def calculer_quantite_necessaire(self, recette_id_str, nb_personnes):
        ingredients_necessaires = {}
        try:
            ingredients_recette = self.get_ingredients_for_recipe(recette_id_str)
            if not ingredients_recette: return {}

            for ing in ingredients_recette:
                try:
                    ing_id = str(ing.get("Ingrédient ok"))
                    if not ing_id or ing_id.lower() in ['nan', 'none', '']: continue
                    qte_str = str(ing.get("Qté/pers_s", "0")).replace(',', '.')
                    qte_par_personne = float(qte_str)
                    ingredients_necessaires[ing_id] = qte_par_personne * nb_personnes
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug(f"Erreur calcul quantité ingrédient {ing.get('Ingrédient ok')} pour recette {recette_id_str}: {e}. Qté str: '{ing.get('Qté/pers_s')}'")
                    continue
            return ingredients_necessaires
        except Exception as e:
            logger.error(f"Erreur globale calculer_quantite_necessaire pour {recette_id_str}: {e}")
            return {}

    def evaluer_disponibilite_et_manquants(self, recette_id_str, nb_personnes):
        ingredients_necessaires = self.calculer_quantite_necessaire(recette_id_str, nb_personnes)
        if not ingredients_necessaires: return 0, 0, {}

        total_ingredients_definis = len(ingredients_necessaires)
        ingredients_disponibles_compteur = 0
        score_total_dispo = 0
        ingredients_manquants = {}

        for ing_id, qte_necessaire in ingredients_necessaires.items():
            ing_id_str = str(ing_id)
            ing_stock_df = self.stock_simule[self.stock_simule[COLONNE_ID_INGREDIENT].astype(str) == ing_id_str]

            qte_en_stock = 0.0
            if not ing_stock_df.empty:
                try:
                    qte_en_stock = float(ing_stock_df["Qte reste"].iloc[0])
                except (ValueError, IndexError, KeyError) as e:
                    logger.error(f"Erreur lecture stock pour {ing_id_str} rec {recette_id_str}: {e}")
            else:
                logger.debug(f"Ingrédient {ing_id_str} (recette {recette_id_str}) non trouvé dans stock_simule.")

            ratio_dispo = 0.0
            if qte_necessaire > 0:
                ratio_dispo = min(1.0, qte_en_stock / qte_necessaire)

            if ratio_dispo >= 0.3: ingredients_disponibles_compteur += 1
            score_total_dispo += ratio_dispo

            if qte_en_stock < qte_necessaire:
                quantite_manquante = qte_necessaire - qte_en_stock
                if quantite_manquante > 0:
                    ingredients_manquants[ing_id_str] = quantite_manquante

        pourcentage_dispo = (ingredients_disponibles_compteur / total_ingredients_definis) * 100 if total_ingredients_definis > 0 else 0
        score_moyen_dispo = score_total_dispo / total_ingredients_definis if total_ingredients_definis > 0 else 0

        logger.debug(f"Éval recette {recette_id_str}: Score={score_moyen_dispo:.2f}, %Dispo={pourcentage_dispo:.0f}%")
        return score_moyen_dispo, pourcentage_dispo, ingredients_manquants

# ── LOGIQUE PRINCIPALE POUR LA LISTE DE COURSES ───────────────────
def generer_liste_de_courses(menu_genere, recettes_df, ingredients_df, rm, nb_personnes):
    """
    Génère un DataFrame pour la liste de courses en se basant sur le menu généré.
    Calcule Qté stock, Qté menus et Qté à acheter pour chaque ingrédient.
    """
    liste_courses = {}

    # 1. Agréger les besoins en ingrédients pour tout le menu
    for recette_id_str in menu_genere.values():
        if recette_id_str:
            ingredients_necessaires = rm.calculer_quantite_necessaire(recette_id_str, nb_personnes)
            for ing_id, qte_necessaire in ingredients_necessaires.items():
                if ing_id not in liste_courses:
                    liste_courses[ing_id] = {"qte_menus": 0, "nom": "", "unite": ""}
                liste_courses[ing_id]["qte_menus"] += qte_necessaire

    # 2. Récupérer le stock et calculer la quantité à acheter
    rows = []
    ingredients_df_indexed = ingredients_df.set_index("Page_ID")
    for ing_id, valeurs in liste_courses.items():
        if ing_id in ingredients_df_indexed.index:
            nom_ingredient = ingredients_df_indexed.loc[ing_id, "Nom"]
            unite = ingredients_df_indexed.loc[ing_id, "unité"]
            # La Qte stock est la Qte reste de l'ingrédient
            qte_stock = float(ingredients_df_indexed.loc[ing_id, "Qte reste"])
            qte_menus = valeurs["qte_menus"]
            qte_a_acheter = max(0, qte_menus - qte_stock)

            if qte_a_acheter > 0:
                rows.append([nom_ingredient, qte_stock, qte_menus, qte_a_acheter, unite])

    # 3. Créer le DataFrame final et l'afficher
    if rows:
        df_courses = pd.DataFrame(rows, columns=["Ingrédient", "Qté stock", "Qté menus", "Qté à acheter", "Unité"])
        df_courses = df_courses.sort_values(by="Ingrédient")

        st.subheader("🛒 Votre liste de courses")

        # Formatter les colonnes pour une meilleure lisibilité
        df_courses["Qté stock"] = df_courses["Qté stock"].apply(lambda x: f"{x:.2f}").str.replace('.', ',')
        df_courses["Qté menus"] = df_courses["Qté menus"].apply(lambda x: f"{x:.2f}").str.replace('.', ',')
        df_courses["Qté à acheter"] = df_courses["Qté à acheter"].apply(lambda x: f"{x:.2f}").str.replace('.', ',')

        st.dataframe(df_courses, hide_index=True)
    else:
        st.subheader("🛒 Votre liste de courses")
        st.success("Vous avez tous les ingrédients nécessaires pour les recettes sélectionnées !")

# ── LOGIQUE D'AFFICHAGE ET APPEL DE FONCTION ──────────────────────
def run_streamlit_app():
    st.set_page_config(page_title="Générateur de Menus", layout="wide")
    st.title("🍽️ Générateur de Menus")

    # Bouton de rechargement des données
    if 'df_recettes' not in st.session_state or st.button('Recharger les données depuis Notion'):
        with st.spinner('Chargement des données depuis Notion...'):
            try:
                st.session_state.df_recettes = extract_recettes()
                st.session_state.df_ingredients = extract_ingredients()
                st.session_state.df_ingredients_recettes = extract_ingr_rec()
                st.session_state.df_menus = extract_menus()
                st.session_state.last_load_time = datetime.now()
                # Initialiser les variables de session pour les menus générés
                st.session_state.menu_genere = {}
                st.session_state.recettes_generees = {}
            except Exception as e:
                st.error(f"Une erreur est survenue lors du chargement des données : {e}")
                st.stop()
        st.success(f"Données chargées le {st.session_state.last_load_time.strftime('%Y-%m-%d à %H:%M:%S')}")

    # Vérifier que les DataFrames sont bien dans session_state avant de continuer
    if 'df_recettes' not in st.session_state:
        st.info("Cliquez sur le bouton pour charger les données.")
        return

    df_recettes = st.session_state.df_recettes
    df_ingredients = st.session_state.df_ingredients
    df_ingredients_recettes = st.session_state.df_ingredients_recettes
    df_menus = st.session_state.df_menus

    verifier_colonnes(df_recettes, ["Nom","Temps_total"], "Recettes")
    verifier_colonnes(df_ingredients_recettes, ["Page_ID", "Qté/pers_s", "Ingrédient ok", "Type de stock f"], "Ingrédients recettes")

    rm = RecetteManager(df_recettes, df_ingredients, df_ingredients_recettes)

    st.sidebar.header("Paramètres")
    nb_personnes = st.sidebar.number_input("Nombre de personnes", min_value=1, value=4)
    nb_repas = st.sidebar.number_input("Nombre de repas à générer", min_value=1, max_value=7, value=3)
    type_repas = st.sidebar.selectbox("Type de repas", options=["rapide", "express", "peu importe"])

    if st.button("Générer mon menu"):
        st.session_state.menu_genere = {}
        st.session_state.recettes_generees = {}

        dates_passees = datetime.now() - timedelta(days=NB_JOURS_ANTI_REPETITION)
        df_menus["Date"] = pd.to_datetime(df_menus["Date"], errors='coerce')
        df_menus_recente = df_menus[df_menus["Date"] >= dates_passees]
        recettes_recentes = [r.strip() for r in df_menus_recente["Recette"].str.split(',').explode().dropna().unique()]

        recettes_dispo = df_recettes[
            ~df_recettes[COLONNE_ID_RECETTE].isin(recettes_recentes)
        ].copy()

        if type_repas == "rapide":
            recettes_dispo = recettes_dispo[pd.to_numeric(recettes_dispo[COLONNE_TEMPS_TOTAL], errors='coerce').fillna(0) <= TEMPS_MAX_RAPIDE]
        elif type_repas == "express":
            recettes_dispo = recettes_dispo[pd.to_numeric(recettes_dispo[COLONNE_TEMPS_TOTAL], errors='coerce').fillna(0) <= TEMPS_MAX_EXPRESS]

        if recettes_dispo.empty:
            st.warning("Aucune recette disponible pour les critères donnés.")
            st.session_state.menu_genere = {}
            return

        recettes_dispo['score_dispo'] = recettes_dispo[COLONNE_ID_RECETTE].apply(
            lambda x: rm.evaluer_disponibilite_et_manquants(x, nb_personnes)[0])
        recettes_dispo['score_anti_gaspi'] = recettes_dispo[COLONNE_ID_RECETTE].apply(
            lambda x: 10 if rm.recette_utilise_ingredient_anti_gaspi(x) else 0)
        recettes_dispo['score_total'] = recettes_dispo['score_dispo'] * 0.7 + recettes_dispo['score_anti_gaspi'] * 0.3

        # Sélection des repas
        for i in range(nb_repas):
            if recettes_dispo.empty: break

            recette_selectionnee = recettes_dispo.sort_values(by="score_total", ascending=False).iloc[0]
            st.session_state.recettes_generees[f"Repas {i+1}"] = recette_selectionnee[COLONNE_NOM]
            st.session_state.menu_genere[f"Repas {i+1}"] = recette_selectionnee[COLONNE_ID_RECETTE]

            recettes_dispo = recettes_dispo.drop(recette_selectionnee.name)

        st.success(f"Menu généré avec succès pour {nb_repas} repas.")

    if st.session_state.get('menu_genere') and st.session_state.get('recettes_generees'):
        st.subheader("✨ Votre menu de la semaine")
        for repas, nom_recette in st.session_state.recettes_generees.items():
            st.write(f"- **{repas}** : {nom_recette}")

        # Appel de la nouvelle fonction pour la liste de courses
        generer_liste_de_courses(st.session_state.menu_genere, df_recettes, df_ingredients, rm, nb_personnes)

# Lancer l'application
if __name__ == "__main__":
    run_streamlit_app()
