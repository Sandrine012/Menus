import streamlit as st
import pandas as pd
import random
import logging
import gdown
import tempfile
import os
from datetime import datetime, timedelta
import time, httpx
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError

# --- NOUVELLES IMPORTATIONS POUR GOOGLE DRIVE ---
import requests
import io
import re

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

            # Vérification si la recette est une bonne candidate anti-gaspi
            utilise_anti_gaspi = self.recette_manager.recette_utilise_ingredient_anti_gaspi(recette_id_str_cand)
            
            # Application de la logique d'inclusion des recettes
            if pourcentage_dispo >= 30 and score_dispo > 0.5:
                # Calcul du score final basé sur les critères
                frequence_historique = self._get_historical_frequency(recette_id_str_cand)
                score_final = score_dispo * (1 / (1 + frequence_historique))

                if utilise_anti_gaspi:
                    # On donne un bonus aux recettes anti-gaspi
                    score_final *= 1.5
                    anti_gaspi_candidates.append({
                        "id": recette_id_str_cand,
                        "nom": nom_recette_cand,
                        "score": score_final,
                        "manquants": manquants_pour_cette_recette,
                        "pourcentage_dispo": pourcentage_dispo,
                        "score_dispo": score_dispo
                    })
                else:
                    candidates.append({
                        "id": recette_id_str_cand,
                        "nom": nom_recette_cand,
                        "score": score_final,
                        "manquants": manquants_pour_cette_recette,
                        "pourcentage_dispo": pourcentage_dispo,
                        "score_dispo": score_dispo
                    })
        
        candidates.sort(key=lambda x: x["score"], reverse=True)
        anti_gaspi_candidates.sort(key=lambda x: x["score"], reverse=True)
        
        return candidates, anti_gaspi_candidates, recettes_ingredients_manquants

    def selectionner_recette(self, candidates, anti_gaspi_candidates, date_repas, nb_personnes, menu_optimal=False):
        """
        Sélectionne une recette parmi les candidats disponibles.
        Priorité aux recettes anti-gaspi si disponibles et si le stock est suffisant.
        """
        liste_candidates = []
        
        # Logique de sélection basée sur la stratégie.
        if anti_gaspi_candidates:
            # Privilégier les recettes anti-gaspi en premier
            liste_candidates = anti_gaspi_candidates + candidates
        else:
            # Sinon, se rabattre sur les candidats normaux
            liste_candidates = candidates

        for c in liste_candidates:
            recette_id = c['id']
            manquants = c['manquants']
            
            # Dans le mode menu optimal, on peut accepter des recettes avec des ingrédients manquants
            if menu_optimal:
                return recette_id, manquants

            # Pour un repas normal, on priorise les recettes sans manquants
            if not manquants:
                return recette_id, manquants
        
        # Si aucune recette sans manquant n'a été trouvée, on retourne la meilleure recette (la première de la liste triée)
        if liste_candidates:
            return liste_candidates[0]['id'], liste_candidates[0]['manquants']

        return None, {}

    def generer_menus(self):
        menus_planifies = pd.DataFrame(columns=["Date", "Repas", "Participants", "Recette choisie", "Ingrédients manquants", "Score disponibilité"])
        liste_de_courses = {}
        menus_par_date = {}
        used_recipes_in_current_gen = set()
        
        df_planning_sorted = self.df_planning.sort_values(by='Date')

        for index, row in df_planning_sorted.iterrows():
            date_repas = row["Date"]
            repas = row["Repas"]
            participants = str(row["Participants"]) if pd.notna(row["Participants"]) else "A"
            transportable_req = str(row["Transportable"]) if pd.notna(row["Transportable"]) else ""
            temps_req = str(row["Temps"]) if pd.notna(row["Temps"]) else ""
            nutrition_req = str(row["Nutrition"]) if pd.notna(row["Nutrition"]) else ""

            nb_personnes = self.compter_participants(participants)

            logger.info(f"Traitement du repas: {repas} le {date_repas.strftime('%d/%m/%Y')}")

            # Générer les candidats pour ce repas
            candidates, anti_gaspi_candidates, recettes_ingredients_manquants = self.generer_recettes_candidates(
                date_repas,
                participants,
                used_recipes_in_current_gen,
                transportable_req,
                temps_req,
                nutrition_req
            )

            recette_choisie_id, manquants = self.selectionner_recette(
                candidates,
                anti_gaspi_candidates,
                date_repas,
                nb_personnes
            )

            if recette_choisie_id:
                recette_choisie_nom = self.recette_manager.obtenir_nom(recette_choisie_id)
                used_recipes_in_current_gen.add(recette_choisie_id)

                # Évaluation finale de la recette choisie
                score_dispo, _, _ = self.recette_manager.evaluer_disponibilite_et_manquants(recette_choisie_id, nb_personnes)

                # Décrémenter le stock si ce n'est pas une simulation
                if not self.ne_pas_decrementer_stock:
                    ing_consommes = self.recette_manager.decrementer_stock(recette_choisie_id, nb_personnes, date_repas)
                    
                    # Logique pour la liste de courses
                    for ing_id, qte_manquante in manquants.items():
                        ing_nom = self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)
                        ing_unite = self.recette_manager.obtenir_unite_ingredient_par_id(ing_id)
                        if ing_nom not in liste_de_courses:
                            liste_de_courses[ing_nom] = [qte_manquante, ing_unite]
                        else:
                            liste_de_courses[ing_nom][0] += qte_manquante
                else:
                    # En mode simulation, calculer les manquants pour la liste de courses
                    for ing_id, qte_manquante in manquants.items():
                        ing_nom = self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)
                        ing_unite = self.recette_manager.obtenir_unite_ingredient_par_id(ing_id)
                        if ing_nom not in liste_de_courses:
                            liste_de_courses[ing_nom] = [qte_manquante, ing_unite]
                        else:
                            liste_de_courses[ing_nom][0] += qte_manquante

                # Enregistrer le résultat dans le DataFrame
                menus_planifies.loc[len(menus_planifies)] = [
                    date_repas,
                    repas,
                    participants,
                    recette_choisie_nom,
                    ", ".join([f"{self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)} ({qte:.2f})" for ing_id, qte in manquants.items()]),
                    f"{score_dispo:.2f}"
                ]
                
                # Enregistrer les détails par date
                if date_repas.strftime('%d/%m/%Y') not in menus_par_date:
                    menus_par_date[date_repas.strftime('%d/%m/%Y')] = {}
                menus_par_date[date_repas.strftime('%d/%m/%Y')][repas] = {
                    'Nom de la recette': recette_choisie_nom,
                    'Ingrédients manquants': {self.recette_manager.obtenir_nom_ingredient_par_id(k): (v, self.recette_manager.obtenir_unite_ingredient_par_id(k)) for k, v in manquants.items()}
                }
                
                logger.info(f"Recette choisie pour {repas} : {recette_choisie_nom} (Score dispo: {score_dispo:.2f})")
            else:
                menus_planifies.loc[len(menus_planifies)] = [
                    date_repas,
                    repas,
                    participants,
                    "Pas de recette trouvée",
                    "",
                    "0.00"
                ]
                if date_repas.strftime('%d/%m/%Y') not in menus_par_date:
                    menus_par_date[date_repas.strftime('%d/%m/%Y')] = {}
                menus_par_date[date_repas.strftime('%d/%m/%Y')][repas] = {
                    'Nom de la recette': "Pas de recette trouvée",
                    'Ingrédients manquants': {}
                }
                logger.warning(f"Pas de recette trouvée pour le repas {repas} le {date_repas.strftime('%d/%m/%Y')}")

        return menus_planifies, liste_de_courses, menus_par_date

def get_gdrive_file_id(url):
    """Extrait l'ID du fichier à partir d'une URL Google Drive."""
    match = re.search(r'id=([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)
    
    match = re.search(r'd/([a-zA-Z0-9_-]+)/', url)
    if match:
        return match.group(1)
    
    return None

def load_data_from_gdrive(url):
    """Charge un fichier CSV depuis une URL Google Drive."""
    try:
        file_id = get_gdrive_file_id(url)
        if not file_id:
            st.error("URL Google Drive invalide. L'ID du fichier n'a pas pu être extrait.")
            return None
        
        download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        
        response = requests.get(download_url)
        response.raise_for_status()
        
        df = pd.read_csv(io.StringIO(response.text))
        st.success("Fichier Google Drive chargé avec succès!")
        return df
    except requests.exceptions.HTTPError as e:
        st.error(f"Erreur de téléchargement du fichier : {e.response.status_code} - {e.response.reason}. Vérifiez que le fichier est public et que l'URL est correcte.")
        return None
    except Exception as e:
        st.error(f"Une erreur est survenue lors du chargement depuis Google Drive : {e}")
        return None

# ────── FONCTION PRINCIPALE POUR L'INTERFACE UTILISATEUR STREAMLIT ────
def main():
    st.set_page_config(page_title="Générateur de Menus Automatique", layout="wide")
    st.title("🍽️ Générateur de Menus Automatique")

    # Initialisation des variables de session
    if 'menus_planifies' not in st.session_state:
        st.session_state['menus_planifies'] = pd.DataFrame()
    if 'liste_de_courses' not in st.session_state:
        st.session_state['liste_de_courses'] = {}
    if 'menus_par_date' not in st.session_state:
        st.session_state['menus_par_date'] = {}
    if 'df_planning_raw' not in st.session_state:
        st.session_state['df_planning_raw'] = pd.DataFrame()
    if 'ne_pas_decrementer_stock' not in st.session_state:
        st.session_state['ne_pas_decrementer_stock'] = True
    if 'params' not in st.session_state:
        st.session_state['params'] = {
            "NB_JOURS_ANTI_REPETITION": NB_JOURS_ANTI_REPETITION_DEFAULT,
            "REPAS_EQUILIBRE": REPAS_EQUILIBRE_DEFAULT,
            "TEMPS_MAX_EXPRESS": TEMPS_MAX_EXPRESS_DEFAULT,
            "TEMPS_MAX_RAPIDE": TEMPS_MAX_RAPIDE_DEFAULT
        }
    if 'url_gdrive' not in st.session_state:
        st.session_state['url_gdrive'] = ""
    
    st.markdown("---")

    # 1. Configuration des paramètres (dans la barre latérale)
    with st.sidebar:
        st.header("Paramètres")
        st.session_state['params']["NB_JOURS_ANTI_REPETITION"] = st.slider(
            "Jours anti-répétition des recettes",
            min_value=1, max_value=90, value=st.session_state['params']["NB_JOURS_ANTI_REPETITION"]
        )
        st.session_state['params']["REPAS_EQUILIBRE"] = st.slider(
            "Calories max pour repas 'équilibré'",
            min_value=300, max_value=2000, value=st.session_state['params']["REPAS_EQUILIBRE"]
        )
        st.session_state['params']["TEMPS_MAX_EXPRESS"] = st.slider(
            "Temps max pour repas 'express' (min)",
            min_value=10, max_value=60, value=st.session_state['params']["TEMPS_MAX_EXPRESS"]
        )
        st.session_state['params']["TEMPS_MAX_RAPIDE"] = st.slider(
            "Temps max pour repas 'rapide' (min)",
            min_value=10, max_value=90, value=st.session_state['params']["TEMPS_MAX_RAPIDE"]
        )
        st.session_state['ne_pas_decrementer_stock'] = st.checkbox(
            "Simuler sans décrémenter le stock (mode lecture seule)",
            value=st.session_state['ne_pas_decrementer_stock']
        )
    
    # 2. Chargement du fichier et génération
    st.header("Chargement du planning")
    
    source_planning = st.radio(
        "Choisissez la source de votre planning :",
        ('Fichier local', 'Google Drive')
    )
    
    df_planning = pd.DataFrame()
    
    if source_planning == 'Fichier local':
        uploaded_file = st.file_uploader("Choisissez un fichier CSV de planning", type="csv")
        if uploaded_file is not None:
            df_planning = pd.read_csv(uploaded_file)
            st.session_state['df_planning_raw'] = df_planning
            st.success("Fichier local chargé avec succès!")
    
    elif source_planning == 'Google Drive':
        gdrive_url = st.text_input("Collez l'URL de votre fichier Google Drive (doit être public) :", value=st.session_state['url_gdrive'])
        st.session_state['url_gdrive'] = gdrive_url
        if gdrive_url:
            if st.button("Charger depuis Google Drive"):
                with st.spinner('Chargement en cours depuis Google Drive...'):
                    df_planning = load_data_from_gdrive(gdrive_url)
                    if df_planning is not None:
                        st.session_state['df_planning_raw'] = df_planning
                        st.success("Fichier Google Drive chargé et stocké dans la session.")
                    else:
                        st.session_state['df_planning_raw'] = pd.DataFrame()
    
    if 'df_planning_raw' in st.session_state and not st.session_state['df_planning_raw'].empty:
        df_planning = st.session_state['df_planning_raw']
        st.write("Aperçu du planning chargé :")
        st.dataframe(df_planning.head())
    else:
        st.info("En attente du chargement d'un fichier de planning.")

    # --- Suite de la logique de génération de menu ---
    if st.button("Générer le menu", type="primary"):
        if 'df_planning_raw' in st.session_state and not st.session_state['df_planning_raw'].empty:
            with st.spinner('Génération en cours...'):
                df_planning = st.session_state['df_planning_raw']
                
                try:
                    # Vérification et traitement du DataFrame
                    verifier_colonnes(df_planning, ["Date", "Repas", "Participants", "Transportable", "Temps", "Nutrition"], "planning")

                    df_planning['Date'] = pd.to_datetime(df_planning['Date'], format='%d/%m/%Y', errors='coerce')
                    df_planning.dropna(subset=['Date'], inplace=True)
                    df_planning = df_planning.sort_values(by='Date')

                    df_recettes = extract_recettes(get_current_season())
                    df_ingredients = extract_ingredients()
                    df_menus_hist = extract_menus()
                    df_ingredients_recettes = extract_ingr_rec()
                    
                    # Vérifier si les DataFrames ne sont pas vides
                    if df_recettes.empty or df_ingredients.empty or df_menus_hist.empty or df_ingredients_recettes.empty:
                        st.error("Une ou plusieurs bases de données Notion sont vides. Veuillez vérifier votre configuration ou l'état de vos bases de données.")
                    else:
                        menu_generator = MenuGenerator(
                            df_menus_hist,
                            df_recettes,
                            df_planning,
                            df_ingredients,
                            df_ingredients_recettes,
                            st.session_state['ne_pas_decrementer_stock'],
                            st.session_state['params']
                        )
                        
                        menus_planifies, liste_de_courses, menus_par_date = menu_generator.generer_menus()
                        
                        st.session_state['menus_planifies'] = menus_planifies
                        st.session_state['liste_de_courses'] = liste_de_courses
                        st.session_state['menus_par_date'] = menus_par_date
                        
                        st.success("Génération du menu terminée avec succès!")
                
                except Exception as e:
                    st.error(f"Une erreur est survenue lors de la génération des menus: {e}")
        else:
            st.error("Veuillez d'abord charger un fichier de planning.")

    st.markdown("---")
    
    # 3. Affichage des résultats
    st.header("Résultats")

    if not st.session_state['menus_planifies'].empty:
        # Affichage du planning généré
        st.subheader("Planning de la semaine généré")
        
        # Style pour le DataFrame
        df_styled = st.session_state['menus_planifies'].style.apply(
            lambda x: ['background-color: #f0f2f6' if i % 2 == 0 else '' for i in range(len(x))], axis=0
        )
        st.dataframe(df_styled, use_container_width=True)

        st.markdown("---")

        # Affichage de la liste de courses
        st.subheader("Liste de courses")
        
        liste_courses_df = pd.DataFrame(st.session_state['liste_de_courses']).T
        liste_courses_df.index.name = "Ingrédient"
        liste_courses_df = liste_courses_df.rename(columns={0: "Quantité manquante", 1: "Unité"})
        
        st.table(liste_courses_df)

        st.markdown("---")
        
        # Affichage des menus individuels
        st.subheader("Détail des menus")
        for date, details in st.session_state['menus_par_date'].items():
            st.markdown(f"**🗓️ {date}**")
            for repas, info in details.items():
                st.markdown(f"**- {repas} :** {info['Nom de la recette']}")
                if info.get('Ingrédients manquants'):
                    manquants_str = ", ".join([f"{qte:.2f} {unite} de {nom}" for nom, (qte, unite) in info['Ingrédients manquants'].items()])
                    st.markdown(f"  _⚠️ Ingrédients manquants :_ {manquants_str}")
            st.markdown("")

    else:
        st.info("Aucun menu n'a encore été généré. Veuillez charger un planning et cliquer sur 'Générer le menu'.")

if __name__ == "__main__":
    main()
