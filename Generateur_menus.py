import streamlit as st
import pandas as pd
import random
import logging
from datetime import datetime, timedelta
import time, httpx
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError
import requests
import io

# ────── CONFIGURATION INITIALE ──────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')
logger = logging.getLogger(__name__)

# Constantes par défaut (seront remplacées par les paramètres de l'utilisateur)
NB_JOURS_ANTI_REPETITION_DEFAULT = 42
REPAS_EQUILIBRE_DEFAULT = 700
TEMPS_MAX_EXPRESS_DEFAULT = 20
TEMPS_MAX_RAPIDE_DEFAULT = 30
VALEUR_DEFAUT_TEMPS_PREPARATION = 10

# Colonnes
COLONNE_NOM = "Nom"
COLONNE_TEMPS_TOTAL = "Temps_total"
COLONNE_ID_RECETTE = "Page_ID"
COLONNE_ID_INGREDIENT = "Page_ID"
COLONNE_AIME_PAS_PRINCIP = "Aime_pas_princip"

# ────── AJOUT DES DÉPENDANCES NOTION ───────────────────────────
NOTION_API_KEY = st.secrets["notion_api_key"]
ID_RECETTES = st.secrets["notion_database_id_recettes"]
ID_MENUS = st.secrets["notion_database_id_menus"]
ID_INGREDIENTS = st.secrets["notion_database_id_ingredients"]
ID_INGREDIENTS_RECETTES = st.secrets["notion_database_id_ingredients_recettes"]
BATCH_SIZE, MAX_RETRY, WAIT_S = 50, 3, 5
notion = Client(auth=NOTION_API_KEY)

# ────── FONCTION POUR DÉTERMINER LA SAISON ACTUELLE ────────────────
def get_current_season():
    """Détermine la saison actuelle en France."""
    jour = datetime.now().day
    mois = datetime.now().month

    # Dates de début de saison approximatives pour l'hémisphère nord
    if (mois == 3 and jour >= 21) or mois in [4, 5] or (mois == 6 and jour < 21):
        return "Printemps"
    elif (mois == 6 and jour >= 21) or mois in [7, 8] or (mois == 9 and jour < 23):
        return "Été"
    elif (mois == 9 and jour >= 23) or mois in [10, 11] or (mois == 12 and jour < 21):
        return "Automne"
    else:
        return "Hiver"

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
    if k=="number":  return str(p.get("number") or "")
    return ""

def extract_recettes(saison_filtre):
    filt = {"and":[
        {"property":"Elément parent","relation":{"is_empty":True}},
        {"or":[
            {"property":"Saison","multi_select":{"contains":"Toute l'année"}},
            {"property":"Saison","multi_select":{"contains":saison_filtre}},
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
            d=datetime.fromisoformat(pr["Date"]["date"]["start"].replace("Z","+00:00")).isoformat()
        rows.append([nom.strip(), ", ".join(rec_ids), d])
    return pd.DataFrame(rows,columns=HDR_MENUS)

# Ajout de la colonne "Intervalle" pour les ingrédients.
HDR_INGR = ["Page_ID","Nom","Type de stock","unité","Qte reste", "Intervalle"]
def extract_ingredients():
    rows=[]
    for p in paginate(ID_INGREDIENTS):
        pr=p["properties"]
        u_prop = pr.get("unité",{})
        if u_prop.get("type")=="rich_text":
            unite="".join(t["plain_text"] for t in u_prop["rich_text"])
        elif u_prop.get("type")=="select":
            unite=(u_prop["select"] or {}).get("name","")
        else:
            unite=""
        qte_prop = pr.get("Qte reste", {})
        qte = ""
        if qte_prop.get("type") == "formula":
            formula_result = qte_prop.get("formula", {})
            if formula_result.get("type") == "number":
                qte = formula_result.get("number")

        # Extraction de la nouvelle propriété "Intervalle"
        intervalle = prop_val(pr.get("Intervalle"), "number")

        rows.append([
            p["id"],
            "".join(t["plain_text"] for t in pr["Nom"]["title"]),
            (pr["Type de stock"]["select"] or {}).get("name",""),
            unite,
            str(qte or ""),
            intervalle
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

        logger.debug(f"Éval recette {recette_id_str}: Score={score_moyen_dispo:.2f}, %Dispo={pourcentage_dispo:.0f}% d'ingrédients. Manquants: {len(ingredients_manquants)}")
        return score_moyen_dispo, pourcentage_dispo, ingredients_manquants

    def decrementer_stock(self, recette_id_str, nb_personnes, date_repas):
        ingredients_necessaires = self.calculer_quantite_necessaire(recette_id_str, nb_personnes)
        ingredients_consommes_ids = set()

        for ing_id, qte_necessaire in ingredients_necessaires.items():
            ing_id_str = str(ing_id)
            idx_list = self.stock_simule.index[self.stock_simule[COLONNE_ID_INGREDIENT].astype(str) == ing_id_str].tolist()
            if not idx_list:
                logger.debug(f"Ingrédient {ing_id_str} (recette {recette_id_str}) non trouvé dans stock_simule pour décrémentation.")
                continue
            idx = idx_list[0]

            try:
                qte_actuelle = float(self.stock_simule.loc[idx, "Qte reste"])
                if qte_actuelle > 0 and qte_necessaire > 0:
                    qte_a_consommer = min(qte_actuelle, qte_necessaire)
                    nouvelle_qte = qte_actuelle - qte_a_consommer
                    self.stock_simule.loc[idx, "Qte reste"] = nouvelle_qte

                    if qte_a_consommer > 0:
                        ingredients_consommes_ids.add(ing_id_str)
                        logger.debug(f"Stock décrémenté pour {ing_id_str} (recette {recette_id_str}): {qte_actuelle:.2f} -> {nouvelle_qte:.2f} (consommé: {qte_a_consommer:.2f})")
            except (ValueError, KeyError) as e:
                logger.error(f"Erreur décrémentation stock pour {ing_id_str} (recette {recette_id_str}): {e}")

        self.anti_gaspi_ingredients = self._trouver_ingredients_stock_eleve()
        return list(ingredients_consommes_ids)

    def obtenir_nom(self, recette_page_id_str):
        try:
            recette_page_id_str = str(recette_page_id_str)
            if self.df_recettes.index.name == COLONNE_ID_RECETTE:
                return self.df_recettes.loc[recette_page_id_str, COLONNE_NOM]
            else:
                return self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE].astype(str) == recette_page_id_str][COLONNE_NOM].iloc[0]
        except (KeyError, IndexError):
            logger.warning(f"Recette ID {recette_page_id_str} non trouvé dans df_recettes (obtenir_nom).")
            return f"Recette_ID_{recette_page_id_str}"
        except Exception as e:
            logger.error(f"Erreur obtenir_nom pour recette ID {recette_page_id_str}: {e}")
            return None

    def obtenir_nom_ingredient_par_id(self, ing_page_id_str):
        try:
            ing_page_id_str = str(ing_page_id_str)
            nom = self.df_ingredients_initial.loc[self.df_ingredients_initial[COLONNE_ID_INGREDIENT].astype(str) == ing_page_id_str, 'Nom'].iloc[0]
            return nom
        except (IndexError, KeyError):
            logger.warning(f"Nom introuvable pour ingrédient ID: {ing_page_id_str} dans df_ingredients_initial.")
            return f"ID_Ing_{ing_page_id_str}"
        except Exception as e:
            logger.error(f"Erreur obtenir_nom_ingredient_par_id pour {ing_page_id_str}: {e}")
            return None
    
    def obtenir_unite_ingredient_par_id(self, ing_page_id_str):
        try:
            ing_page_id_str = str(ing_page_id_str)
            unite = self.df_ingredients_initial.loc[self.df_ingredients_initial[COLONNE_ID_INGREDIENT].astype(str) == ing_page_id_str, 'unité'].iloc[0]
            return unite
        except (IndexError, KeyError):
            logger.warning(f"Unité introuvable pour ingrédient ID: {ing_page_id_str} dans df_ingredients_initial.")
            return None
        except Exception as e:
            logger.error(f"Erreur obtenir_unite_ingredient_par_id pour {ing_page_id_str}: {e}")
            return None

    def obtenir_qte_stock_par_id(self, ing_page_id_str):
        try:
            ing_page_id_str = str(ing_page_id_str)
            qte_stock = self.stock_simule.loc[self.stock_simule[COLONNE_ID_INGREDIENT].astype(str) == ing_page_id_str, 'Qte reste'].iloc[0]
            return float(qte_stock)
        except (IndexError, KeyError, ValueError):
            return 0.0
        except Exception as e:
            logger.error(f"Erreur obtenir_qte_stock_par_id pour {ing_page_id_str}: {e}")
            return 0.0

    def obtenir_qte_stock_initial_par_id(self, ing_page_id_str):
        try:
            ing_page_id_str = str(ing_page_id_str)
            qte_stock = self.df_ingredients_initial.loc[self.df_ingredients_initial[COLONNE_ID_INGREDIENT].astype(str) == ing_page_id_str, 'Qte reste'].iloc[0]
            return float(qte_stock)
        except (IndexError, KeyError, ValueError):
            return 0.0
        except Exception as e:
            logger.error(f"Erreur obtenir_qte_stock_initial_par_id pour {ing_page_id_str}: {e}")
            return 0.0

    def obtenir_intervalle_ingredient_par_id(self, ing_page_id_str):
        try:
            ing_page_id_str = str(ing_page_id_str)
            intervalle = self.df_ingredients_initial.loc[self.df_ingredients_initial[COLONNE_ID_INGREDIENT].astype(str) == ing_page_id_str, 'Intervalle'].iloc[0]
            return int(intervalle) if pd.notna(intervalle) and str(intervalle).isdigit() else 0
        except (IndexError, KeyError, ValueError):
            logger.debug(f"Intervalle non trouvé ou non valide pour l'ingrédient {ing_page_id_str}. Retourne 0.")
            return 0
        except Exception as e:
            logger.error(f"Erreur lors de l'obtention de l'intervalle pour {ing_page_id_str}: {e}")
            return 0

    def est_adaptee_aux_participants(self, recette_page_id_str, participants_str_codes):
        try:
            recette_page_id_str = str(recette_page_id_str)
            if self.df_recettes.index.name == COLONNE_ID_RECETTE:
                recette_info = self.df_recettes.loc[recette_page_id_str]
            else:
                recette_info = self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE].astype(str) == recette_page_id_str].iloc[0]

            if COLONNE_AIME_PAS_PRINCIP not in recette_info or pd.isna(recette_info[COLONNE_AIME_PAS_PRINCIP]):
                return True
            n_aime_pas = [code.strip() for code in str(recette_info[COLONNE_AIME_PAS_PRINCIP]).split(",") if code.strip()]
            participants_actifs = [code.strip() for code in participants_str_codes.split(",") if code.strip()]
            
            is_adapted = not any(code_participant in n_aime_pas for code_participant in participants_actifs)
            if not is_adapted:
                logger.debug(f"Recette {self.obtenir_nom(recette_page_id_str)} ({recette_page_id_str}) filtrée par participants. Participants actifs: {participants_actifs}, N'aime pas: {n_aime_pas}")
            return is_adapted
        except (KeyError, IndexError):
            logger.warning(f"Recette ID {recette_page_id_str} non trouvée pour vérifier adaptation participants.")
            return True
        except Exception as e:
            logger.error(f"Erreur vérification adaptation participants pour {recette_page_id_str}: {e}")
            return False

    def est_transportable(self, recette_page_id_str):
        try:
            recette_page_id_str = str(recette_page_id_str)
            if self.df_recettes.index.name == COLONNE_ID_RECETTE:
                valeur = str(self.df_recettes.loc[recette_page_id_str, "Transportable"]).strip().lower()
            else:
                valeur = str(self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE].astype(str) == recette_page_id_str]["Transportable"].iloc[0]).strip().lower()
            
            is_transportable = (valeur == "oui")
            if not is_transportable:
                logger.debug(f"Recette {self.obtenir_nom(recette_page_id_str)} ({recette_page_id_str}) filtrée: Non transportable (valeur: '{valeur}')")
            return is_transportable
        except (KeyError, IndexError):
            logger.debug(f"Recette ID {recette_page_id_str} non trouvée pour transportable.")
            return False
        except Exception as e:
            logger.error(f"Erreur vérification transportable pour {recette_page_id_str}: {e}")
            return False

    def obtenir_temps_preparation(self, recette_page_id_str):
        try:
            recette_page_id_str = str(recette_page_id_str)
            if self.df_recettes.index.name == COLONNE_ID_RECETTE:
                recette_info = self.df_recettes.loc[recette_page_id_str]
            else:
                recette_info = self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE].astype(str) == recette_page_id_str].iloc[0]

            if COLONNE_TEMPS_TOTAL in recette_info and pd.notna(recette_info[COLONNE_TEMPS_TOTAL]):
                return int(recette_info[COLONNE_TEMPS_TOTAL])
            return VALEUR_DEFAUT_TEMPS_PREPARATION
        except (KeyError, IndexError):
            logger.debug(f"Recette ID {recette_page_id_str} non trouvée pour temps_preparation.")
            return VALEUR_DEFAUT_TEMPS_PREPARATION
        except (ValueError, TypeError):
            logger.warning(f"Temps de prép non valide pour recette {recette_page_id_str}. Valeur par défaut.")
            return VALEUR_DEFAUT_TEMPS_PREPARATION
        except Exception as e:
            logger.error(f"Erreur obtention temps prép pour {recette_page_id_str}: {e}")
            return VALEUR_DEFAUT_TEMPS_PREPARATION
    
    def obtenir_calories(self, recette_page_id_str):
        try:
            recette_page_id_str = str(recette_page_id_str)
            if self.df_recettes.index.name == COLONNE_ID_RECETTE:
                calories_str = self.df_recettes.loc[recette_page_id_str, "Calories"]
            else:
                calories_str = self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE].astype(str) == recette_page_id_str]["Calories"].iloc[0]
            
            return float(calories_str) if pd.notna(calories_str) and str(calories_str).replace('.', '', 1).isdigit() else 0.0
        except (KeyError, IndexError):
            logger.debug(f"Recette ID {recette_page_id_str} non trouvée pour Calories.")
            return 0.0
        except (ValueError, TypeError):
            logger.warning(f"Calories non valides pour recette {recette_page_id_str}. Valeur par défaut.")
            return 0.0
        except Exception as e:
            logger.error(f"Erreur obtention calories pour {recette_page_id_str}: {e}")
            return 0.0

class MenusHistoryManager:
    """Gère l'accès et les opérations sur l'historique des menus."""
    def __init__(self, df_menus_hist):
        self.df_menus_historique = df_menus_hist.copy()
        self.df_menus_historique["Date"] = pd.to_datetime(self.df_menus_historique["Date"], errors="coerce")
        self.df_menus_historique.dropna(subset=["Date"], inplace=True)
        if 'Date' in self.df_menus_historique.columns:
            self.df_menus_historique['Semaine'] = self.df_menus_historique['Date'].dt.isocalendar().week
            self.recettes_historique_counts = self.df_menus_historique['Recette'].value_counts().to_dict()
        else:
            logger.warning("La colonne 'Date' est manquante dans l'historique des menus, impossible de calculer la semaine.")
            self.recettes_historique_counts = {}

    def is_ingredient_recent(self, ingredient_id_str, date_actuelle, intervalle_jours):
        """Vérifie si un ingrédient a été consommé dans l'intervalle de jours spécifié."""
        try:
            df_hist = self.df_menus_historique
            if df_hist.empty or intervalle_jours <= 0:
                return False

            debut = date_actuelle - timedelta(days=intervalle_jours + 1)
            
            return False # Placeholder
        except Exception as e:
            logger.error(f"Erreur dans is_ingredient_recent pour {ingredient_id_str}: {e}")
            return False

class MenuGenerator:
    """Génère les menus en fonction du planning et des règles."""
    def __init__(self, df_menus_hist, df_recettes, df_planning, df_ingredients, df_ingredients_recettes, ne_pas_decrementer_stock, params):
        self.df_planning = df_planning.copy()
        if "Date" in self.df_planning.columns:
            self.df_planning['Date'] = pd.to_datetime(self.df_planning['Date'], errors='coerce')
            self.df_planning.dropna(subset=['Date'], inplace=True)
        else:
            logger.error("'Date' manquante dans le planning.")
            raise ValueError("Colonne 'Date' manquante dans le fichier de planning.")

        self.recette_manager = RecetteManager(df_recettes, df_ingredients, df_ingredients_recettes)
        self.menus_history_manager = MenusHistoryManager(df_menus_hist)
        self.ne_pas_decrementer_stock = ne_pas_decrementer_stock
        self.params = params

    def recettes_meme_semaine_annees_precedentes(self, date_actuelle):
        try:
            df_hist = self.menus_history_manager.df_menus_historique
            if df_hist.empty or not all(col in df_hist.columns for col in ['Date', 'Semaine', 'Recette']):
                return set()

            semaine_actuelle = date_actuelle.isocalendar()[1]
            annee_actuelle = date_actuelle.year

            df_menus_semaine = df_hist[
                (df_hist["Semaine"].astype(int) == semaine_actuelle) &
                (df_hist["Date"].dt.year < annee_actuelle) &
                pd.notna(df_hist["Recette"])
            ]
            return set(df_menus_semaine["Recette"].astype(str).unique())
        except Exception as e:
            logger.error(f"Erreur recettes_meme_semaine_annees_precedentes pour {date_actuelle}: {e}")
            return set()

    def est_recente(self, recette_page_id_str, date_actuelle):
        try:
            df_hist = self.menus_history_manager.df_menus_historique
            if df_hist.empty or not all(col in df_hist.columns for col in ['Date', 'Recette']):
                return False

            debut = date_actuelle - timedelta(days=self.params["NB_JOURS_ANTI_REPETITION"])
            fin = date_actuelle
            mask = (
                (df_hist['Recette'].astype(str) == str(recette_page_id_str)) &
                (df_hist['Date'] > debut) &
                (df_hist['Date'] <= fin)
            )
            is_recent = not df_hist.loc[mask].empty
            if is_recent:
                logger.debug(f"Recette {self.recette_manager.obtenir_nom(recette_page_id_str)} ({recette_page_id_str}) filtrée: Est récente (dans les {self.params['NB_JOURS_ANTI_REPETITION']} jours)")
            return is_recent

        except Exception as e:
            logger.error(f"Erreur est_recente pour {recette_page_id_str} à {date_actuelle}: {e}")
            return False

    def est_intervalle_respecte(self, recette_page_id_str, date_actuelle):
        try:
            ingredients_recette = self.recette_manager.get_ingredients_for_recipe(recette_page_id_str)
            
            for ing in ingredients_recette:
                ing_id_str = str(ing.get("Ingrédient ok"))
                if not ing_id_str or ing_id_str.lower() in ['nan', 'none', '']: continue

                intervalle_jours = self.recette_manager.obtenir_intervalle_ingredient_par_id(ing_id_str)
                if intervalle_jours <= 0:
                    continue

                df_hist = self.menus_history_manager.df_menus_historique
                if df_hist.empty: continue

                df_ir = self.recette_manager.df_ingredients_recettes
                recettes_utilisant_ing = df_ir[df_ir["Ingrédient ok"].astype(str) == ing_id_str]
                
                if not recettes_utilisant_ing.empty:
                    recette_ids_utilisant_ing = set(recettes_utilisant_ing[COLONNE_ID_RECETTE].astype(str).unique())

                    debut_intervalle = date_actuelle - timedelta(days=intervalle_jours)
                    
                    mask_hist = (
                        (df_hist['Date'] >= debut_intervalle) &
                        (df_hist['Recette'].astype(str).isin(recette_ids_utilisant_ing))
                    )

                    if not df_hist.loc[mask_hist].empty:
                        nom_ing = self.recette_manager.obtenir_nom_ingredient_par_id(ing_id_str)
                        logger.debug(f"Recette {self.recette_manager.obtenir_nom(recette_page_id_str)} filtrée: L'ingrédient '{nom_ing}' a été utilisé récemment (intervalle de {intervalle_jours} jours non respecté).")
                        return False

            return True
        except Exception as e:
            logger.error(f"Erreur est_intervalle_respecte pour {recette_page_id_str} à {date_actuelle}: {e}")
            return True

    def compter_participants(self, participants_str_codes):
        if not isinstance(participants_str_codes, str): return 1
        if participants_str_codes == "B": return 1
        return len([p for p in participants_str_codes.replace(" ", "").split(",") if p])

    def _filtrer_recette_base(self, recette_id_str, participants_str_codes):
        return self.recette_manager.est_adaptee_aux_participants(recette_id_str, participants_str_codes)
    
    def _get_historical_frequency(self, recette_id):
        return self.menus_history_manager.recettes_historique_counts.get(recette_id, 0)

    def generer_recettes_candidates(self, date_repas, participants_str_codes, used_recipes_in_current_gen, transportable_req, temps_req, nutrition_req, exclure_recettes_ids=None):
        if exclure_recettes_ids is None:
            exclure_recettes_ids = set()

        candidates = []
        anti_gaspi_candidates = []
        recettes_scores_dispo = {}
        recettes_ingredients_manquants = {}

        nb_personnes = self.compter_participants(participants_str_codes)

        logger.debug(f"--- Recherche de candidats pour {date_repas.strftime('%Y-%m-%d %H:%M')} (Participants: {participants_str_codes}) ---")

        for recette_id_str_cand in self.recette_manager.df_recettes.index.astype(str):
            nom_recette_cand = self.recette_manager.obtenir_nom(recette_id_str_cand)

            if recette_id_str_cand in exclure_recettes_ids:
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Exclu par le menu Optimal.")
                continue

            # CORRECTION DU COMPORTEMENT : Appliquer le filtre seulement si la contrainte est spécifiée
            if str(transportable_req).strip().lower() == "oui" and not self.recette_manager.est_transportable(recette_id_str_cand):
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Non transportable pour une demande transportable.")
                continue

            temps_total = self.recette_manager.obtenir_temps_preparation(recette_id_str_cand)
            if temps_req == "express" and temps_total > self.params['TEMPS_MAX_EXPRESS']:
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Temps ({temps_total} min) > Express ({self.params['TEMPS_MAX_EXPRESS']} min).")
                continue
            if temps_req == "rapide" and temps_total > self.params['TEMPS_MAX_RAPIDE']:
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Temps ({temps_total} min) > Rapide ({self.params['TEMPS_MAX_RAPIDE']} min).")
                continue

            if nutrition_req == "équilibré":
                try:
                    calories = self.recette_manager.obtenir_calories(recette_id_str_cand)
                    if calories > self.params['REPAS_EQUILIBRE']:
                        logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Calories ({calories}) > Équilibré ({self.params['REPAS_EQUILIBRE']}).")
                        continue
                except Exception:
                    logger.debug(f"Calories non valides/trouvées pour {nom_recette_cand} ({recette_id_str_cand}) (filtre nutrition).")
                    continue
            
            if recette_id_str_cand in used_recipes_in_current_gen:
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Déjà utilisé dans la génération actuelle.")
                continue

            if not self._filtrer_recette_base(recette_id_str_cand, participants_str_codes):
                continue

            if self.est_recente(recette_id_str_cand, date_repas):
                continue

            if not self.est_intervalle_respecte(recette_id_str_cand, date_repas):
                continue

            score_dispo, pourcentage_dispo, manquants_pour_cette_recette = self.recette_manager.evaluer_disponibilite_et_manquants(recette_id_str_cand, nb_personnes)
            recettes_scores_dispo[recette_id_str_cand] = score_dispo
            recettes_ingredients_manquants[recette_id_str_cand] = manquants_pour_cette_recette
            candidates.append(recette_id_str_cand)
            logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) ajouté: Score dispo {score_dispo:.2f}, {pourcentage_dispo:.0f}% d'ingrédients. Manquants: {len(manquants_pour_cette_recette)}")

            if self.recette_manager.recette_utilise_ingredient_anti_gaspi(recette_id_str_cand):
                anti_gaspi_candidates.append(recette_id_str_cand)
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) est aussi anti-gaspi.")

        if not candidates:
            logger.debug("Aucun candidat trouvé après le filtrage initial.")
            return [], {}

        if exclure_recettes_ids:
            candidates_triees = sorted(candidates, key=lambda r_id: self._get_historical_frequency(r_id))
        else:
            candidates_triees = sorted(candidates, key=lambda r_id: recettes_scores_dispo.get(r_id, -1), reverse=True)
        
        anti_gaspi_triees = sorted(anti_gaspi_candidates, key=lambda r_id: recettes_scores_dispo.get(r_id, -1), reverse=True)

        if anti_gaspi_triees and recettes_scores_dispo.get(anti_gaspi_triees[0], -1) >= 0.5:
            logger.debug(f"Priorisation des candidats anti-gaspi (meilleur score {recettes_scores_dispo.get(anti_gaspi_triees[0], -1):.2f}).")
            return anti_gaspi_triees[:5], recettes_ingredients_manquants

        logger.debug(f"Retourne les {min(len(candidates_triees), 10)} meilleurs candidats.")
        return candidates_triees[:10], recettes_ingredients_manquants


    def _traiter_menu_standard(self, date_repas, participants_str_codes, participants_count_int, used_recipes_in_current_gen_set, menu_recent_noms_list, transportable_req_str, temps_req_str, nutrition_req_str, exclure_recettes_ids=None):
        logger.debug(f"--- Traitement Repas Standard pour {date_repas.strftime('%Y-%m-%d %H:%M')} ---")
        recettes_candidates_initiales, recettes_manquants_dict = self.generer_recettes_candidates(
            date_repas, participants_str_codes, used_recipes_in_current_gen_set, transportable_req_str, temps_req_str, nutrition_req_str, exclure_recettes_ids=exclure_recettes_ids
        )

        if not recettes_candidates_initiales:
            logger.debug(f"Aucune recette candidate initiale pour {date_repas.strftime('%Y-%m-%d %H:%M')}.")
            return None, {}

        recettes_historiques_semaine_set = self.recettes_meme_semaine_annees_precedentes(date_repas)

        scores_candidats_dispo = {
            r_id: self.recette_manager.evaluer_disponibilite_et_manquants(r_id, participants_count_int)[0]
            for r_id in recettes_candidates_initiales
        }

        preferred_candidates_list = [r_id for r_id in recettes_candidates_initiales if r_id in recettes_historiques_semaine_set]
        if preferred_candidates_list:
            logger.debug(f"{len(preferred_candidates_list)} candidats préférés (historique semaine précédente) trouvés.")

        mots_cles_exclus_set = set()
        if menu_recent_noms_list:
            for nom_plat_recent in menu_recent_noms_list:
                if isinstance(nom_plat_recent, str) and nom_plat_recent.strip():
                    try:
                        mots_cles_exclus_set.add(nom_plat_recent.lower().split()[0])
                    except IndexError:
                        pass
        if mots_cles_exclus_set:
            logger.debug(f"Mots-clés exclus basés sur les repas récents : {mots_cles_exclus_set}")

        filtered_candidates = [
            r_id for r_id in recettes_candidates_initiales
            if not any(mots_cles_exclus_set.intersection(self.recette_manager.obtenir_nom(r_id).lower().split()))
        ]

        if not filtered_candidates and recettes_candidates_initiales:
            logger.debug("Le filtrage par mots-clés exclus a éliminé tous les candidats. Revenir à la liste initiale.")
            filtered_candidates = recettes_candidates_initiales

        if preferred_candidates_list:
            final_candidates = preferred_candidates_list
        else:
            final_candidates = filtered_candidates
            
        final_candidates_scores = {r_id: scores_candidats_dispo.get(r_id, 0.0) for r_id in final_candidates}
        sorted_candidates = sorted(final_candidates, key=lambda r_id: final_candidates_scores[r_id], reverse=True)
        
        selected_recette_id = sorted_candidates[0] if sorted_candidates else None
        if selected_recette_id:
            logger.debug(f"Recette sélectionnée pour {date_repas.strftime('%Y-%m-%d %H:%M')}: {self.recette_manager.obtenir_nom(selected_recette_id)}")
            return selected_recette_id, recettes_manquants_dict.get(selected_recette_id, {})
        
        return None, {}

    def generer_semaine_menus(self):
        st.subheader("Génération des menus")
        menus_semaine = {}
        ingredients_manquants_globaux = {}
        jours_a_generer = self.df_planning.groupby('Date').first().index.tolist()
        used_recipes_in_current_gen_set = set()
        menu_recent_noms_list = []

        with st.spinner("Génération des menus en cours..."):
            for date_repas in jours_a_generer:
                df_jour = self.df_planning[self.df_planning['Date'] == date_repas]
                menus_jour = {}
                for index, row in df_jour.iterrows():
                    repas_type = row['Repas']
                    participants = row['Participants']
                    transportable = row.get('Transportable', 'Non')
                    temps = row.get('Temps', 'Normal')
                    nutrition = row.get('Nutrition', 'Normal')
                    repas_fixe_id = row.get('Recette', None)

                    if pd.notna(repas_fixe_id) and repas_fixe_id:
                        selected_recette_id = str(repas_fixe_id)
                        manquants = self.recette_manager.evaluer_disponibilite_et_manquants(selected_recette_id, self.compter_participants(participants))[2]
                        logger.debug(f"Recette fixe pour {repas_type} à {date_repas} : {self.recette_manager.obtenir_nom(selected_recette_id)}")
                    else:
                        selected_recette_id, manquants = self._traiter_menu_standard(
                            date_repas, participants, self.compter_participants(participants), used_recipes_in_current_gen_set, menu_recent_noms_list,
                            transportable, temps, nutrition
                        )
                    
                    if selected_recette_id:
                        selected_recette_name = self.recette_manager.obtenir_nom(selected_recette_id)
                        menus_jour[repas_type] = {
                            "Nom": selected_recette_name,
                            "ID": selected_recette_id,
                            "Participants": participants,
                            "Manquants": manquants
                        }
                        used_recipes_in_current_gen_set.add(selected_recette_id)
                        menu_recent_noms_list.append(selected_recette_name)

                        if not self.ne_pas_decrementer_stock:
                            consommes = self.recette_manager.decrementer_stock(selected_recette_id, self.compter_participants(participants), date_repas)

                        for ing_id, qte_manquante in manquants.items():
                            nom_ing = self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)
                            unite_ing = self.recette_manager.obtenir_unite_ingredient_par_id(ing_id)
                            if nom_ing not in ingredients_manquants_globaux:
                                ingredients_manquants_globaux[nom_ing] = {"quantite": qte_manquante, "unite": unite_ing, "recettes": []}
                            else:
                                ingredients_manquants_globaux[nom_ing]["quantite"] += qte_manquante
                            
                            recette_info = {"recette": selected_recette_name, "date": date_repas.strftime("%d/%m")}
                            if recette_info not in ingredients_manquants_globaux[nom_ing]["recettes"]:
                                ingredients_manquants_globaux[nom_ing]["recettes"].append(recette_info)

                    else:
                        menus_jour[repas_type] = {"Nom": "Pas de recette trouvée", "ID": None, "Participants": participants}
                menus_semaine[date_repas] = menus_jour
            
        return menus_semaine, ingredients_manquants_globaux

def load_planning_from_google_drive(file_id):
    """Charge le fichier Planning.csv depuis Google Drive."""
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    try:
        response = requests.get(url)
        response.raise_for_status()  # Lève une exception pour les codes d'erreur HTTP
        csv_content = io.BytesIO(response.content)
        df = pd.read_csv(csv_content, sep=';', encoding='utf-8')
        st.success("Fichier de planning téléchargé avec succès depuis Google Drive!")
        return df
    except requests.exceptions.RequestException as e:
        st.error(f"Erreur lors du téléchargement du fichier depuis Google Drive: {e}")
        return None
    except pd.errors.ParserError as e:
        st.error(f"Erreur de lecture du fichier CSV. Veuillez vérifier son format. Détails: {e}")
        return None

def main():
    st.title("Générateur de menus Streamlit")
    st.markdown("---")

    # Section de téléchargement de Planning.csv
    file_id = "1nIRFvCVFqbc3Ca8YhSWDajWIG7np06X8"
    df_planning = load_planning_from_google_drive(file_id)

    if df_planning is None:
        st.warning("Impossible de continuer sans le fichier de planning.")
        st.stop()

    # Le reste de votre application Streamlit commence ici
    # (le code est le même que l'original, mais il faut s'assurer que
    # les DataFrames sont bien chargés)
    
    # 1. Extraction des données de Notion
    st.info("Extraction des données de Notion...")
    df_recettes = extract_recettes(get_current_season())
    df_menus_hist = extract_menus()
    df_ingredients = extract_ingredients()
    df_ingredients_recettes = extract_ingr_rec()

    st.success("Données Notion extraites avec succès.")

    # 2. Vérification des colonnes
    try:
        verifier_colonnes(df_planning, ['Date', 'Repas', 'Participants'], nom_fichier="Planning.csv")
    except ValueError as e:
        st.error(f"Erreur de format du fichier Planning.csv : {e}")
        st.stop()

    st.sidebar.header("Paramètres")
    ne_pas_decrementer_stock = st.sidebar.checkbox("Ne pas décrémenter le stock", value=False)
    nb_jours_anti_repetition = st.sidebar.slider("Nombre de jours anti-répétition", 1, 90, NB_JOURS_ANTI_REPETITION_DEFAULT)
    repas_equilibre_val = st.sidebar.number_input("Calories max pour 'équilibré'", min_value=100, value=REPAS_EQUILIBRE_DEFAULT)
    temps_max_express_val = st.sidebar.number_input("Temps max 'express' (min)", min_value=5, value=TEMPS_MAX_EXPRESS_DEFAULT)
    temps_max_rapide_val = st.sidebar.number_input("Temps max 'rapide' (min)", min_value=5, value=TEMPS_MAX_RAPIDE_DEFAULT)

    params = {
        "NB_JOURS_ANTI_REPETITION": nb_jours_anti_repetition,
        "REPAS_EQUILIBRE": repas_equilibre_val,
        "TEMPS_MAX_EXPRESS": temps_max_express_val,
        "TEMPS_MAX_RAPIDE": temps_max_rapide_val
    }

    if st.button("Générer les menus"):
        try:
            generator = MenuGenerator(
                df_menus_hist, df_recettes, df_planning, df_ingredients, df_ingredients_recettes, ne_pas_decrementer_stock, params
            )
            menus_semaine, ingredients_manquants = generator.generer_semaine_menus()
            
            st.subheader("Menus Générés")
            # Affichage des menus (code d'affichage à compléter)
            
            st.subheader("Ingrédients manquants")
            if ingredients_manquants:
                df_manquants = pd.DataFrame([
                    {"Ingrédient": ing, "Quantité": val["quantite"], "Unité": val["unite"], "Recettes": ", ".join([r["recette"] for r in val["recettes"]])}
                    for ing, val in ingredients_manquants.items()
                ])
                st.table(df_manquants)
            else:
                st.info("Aucun ingrédient manquant trouvé.")
        except Exception as e:
            st.error(f"Une erreur est survenue lors de la génération des menus: {e}")

if __name__ == "__main__":
    main()
