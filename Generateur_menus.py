import streamlit as st
import pandas as pd
import random
import logging
from datetime import datetime, timedelta
import time, httpx
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError
from dateutil import parser

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
            d=datetime.fromisoformat(pr["Date"]["date"]["start"].replace("Z","+00:00"))
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
    
            def parse_date(date_str):
                if pd.isna(date_str) or not date_str:
                    return pd.NaT
                try:
                    return parser.parse(date_str).date()
                except Exception:
                    return pd.NaT
    
            self.df_menus_historique["Date"] = self.df_menus_historique["Date"].apply(parse_date)
            self.df_menus_historique.dropna(subset=["Date"], inplace=True)
            self.df_menus_historique["Date"] = pd.to_datetime(self.df_menus_historique["Date"])
    
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
            # Correction : Spécifiez le format de date
            self.df_planning['Date'] = pd.to_datetime(self.df_planning['Date'], format='%d/%m/%Y', errors='coerce')
            self.df_planning.dropna(subset=['Date'], inplace=True)
        else:
            logger.error("'Date' manquante dans le planning.")
            raise ValueError("Colonne 'Date' manquante dans le fichier de planning.")

        self.recette_manager = RecetteManager(df_recettes, df_ingredients, df_ingredients_recettes)
        self.menus_history_manager = MenusHistoryManager(df_menus_hist)
        self.ne_pas_decrementer_stock = ne_pas_decrementer_stock
        self.params = params

        # Ajout de ces listes pour suivre les recettes et les mots-clés de la semaine
        self.recettes_selectionnees_semaine = []
        self.mots_cles_selectionnes_semaine = set() # Utilisation d'un set pour une meilleure performance
        
    def recettes_meme_semaine_annees_precedentes(self, date_actuelle):
        # ... (Cette méthode reste inchangée)
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
    
    def est_recente(self, recette_id, date_actuelle):
        # ... (Cette méthode reste inchangée)
        try:
            df_hist = self.menus_history_manager.df_menus_historique
            dates_recette = df_hist[df_hist['Recette'].astype(str) == str(recette_id)]['Date']
            if dates_recette.empty:
                return False
            start_date = date_actuelle - timedelta(days=self.params["NB_JOURS_ANTI_REPETITION"])
            return any((d >= start_date) for d in dates_recette)
        except Exception as e:
            logger.error(f"Erreur dans la vérification de la fraîcheur de la recette {recette_id}: {e}")
            return False

    def est_intervalle_respecte(self, recette_page_id_str, date_actuelle):
        # ... (Cette méthode reste inchangée)
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
        # ... (Cette méthode reste inchangée)
        if not isinstance(participants_str_codes, str): return 1
        if participants_str_codes == "B": return 1
        return len([p for p in participants_str_codes.replace(" ", "").split(",") if p])

    def _filtrer_recette_base(self, recette_id_str, participants_str_codes):
        # ... (Cette méthode reste inchangée)
        return self.recette_manager.est_adaptee_aux_participants(recette_id_str, participants_str_codes)
    
    def _get_historical_frequency(self, recette_id):
        # ... (Cette méthode reste inchangée)
        return self.menus_history_manager.recettes_historique_counts.get(recette_id, 0)
    
def generer_recettes_candidates(
    self,
    date_repas,
    participants_str_codes,
    used_recipes_in_current_gen,
    transportable_req,
    temps_req,
    nutrition_req,
    exclure_recettes_ids=None,
    ingredients_utilises_cette_semaine=None):
    if exclure_recettes_ids is None:
        exclure_recettes_ids = set()
    if ingredients_utilises_cette_semaine is None:
        ingredients_utilises_cette_semaine = {}

    candidates = []
    anti_gaspi_candidates = []
    recettes_scores_dispo = {}
    recettes_ingredients_manquants = {}
    nb_personnes = self.compter_participants(participants_str_codes)

    for recette_id_str_cand in self.recette_manager.df_recettes.index.astype(str):
        nom_recette_cand = self.recette_manager.obtenir_nom(recette_id_str_cand)
        if recette_id_str_cand in exclure_recettes_ids:
            continue

        mot_cle_recette = nom_recette_cand.split()[0].lower()
        if mot_cle_recette in self.mots_cles_selectionnes_semaine:
            continue

        # --- CORRECTIF INTERVALLE INGREDIENT ---
        ingredients_recette_cand = {i.get("Ingrédient ok") for i in self.recette_manager.get_ingredients_for_recipe(recette_id_str_cand) if i.get("Ingrédient ok")}
        is_interval_ok = True
        for ing_id in ingredients_recette_cand:
            # Utilisation de la méthode CORRECTE
            intervalle_jrs_ing = self.recette_manager.obtenir_intervalle_ingredient_par_id(ing_id)
            if intervalle_jrs_ing is not None and intervalle_jrs_ing > 0:
                # Vérifie utilisation dans la génération en cours
                if ing_id in ingredients_utilises_cette_semaine:
                    date_derniere_utilisation = ingredients_utilises_cette_semaine[ing_id]
                    jours_ecoules = (date_repas.date() - date_derniere_utilisation.date()).days
                    if jours_ecoules < intervalle_jrs_ing:
                        is_interval_ok = False
                        break
                # Vérifie utilisation dans l'historique
                debut_check = date_repas - timedelta(days=intervalle_jrs_ing)
                df_hist = self.menus_history_manager.df_menus_historique
                if not df_hist.empty:
                    df_ir = self.recette_manager.df_ingredients_recettes
                    recettes_utilisant_ing = df_ir[df_ir["Ingrédient ok"].astype(str) == str(ing_id)]
                    if not recettes_utilisant_ing.empty:
                        ids_recettes = set(recettes_utilisant_ing[COLONNE_ID_RECETTE].astype(str))
                        mask = (df_hist["Recette"].astype(str).isin(ids_recettes)) & (df_hist["Date"] >= debut_check)
                        if not df_hist.loc[mask].empty:
                            is_interval_ok = False
                            break
        if not is_interval_ok:
            continue

        # --- FIN CORRECTIF INTERVALLE ---
        # ... Les autres filtres inchangés ...
        if str(transportable_req).strip().lower() == "oui" and not self.recette_manager.est_transportable(recette_id_str_cand):
            continue
        temps_total = self.recette_manager.obtenir_temps_preparation(recette_id_str_cand)
        if (temps_req == "express" and temps_total > self.params['TEMPS_MAX_EXPRESS']) or \
           (temps_req == "rapide" and temps_total > self.params['TEMPS_MAX_RAPIDE']):
            continue
        if nutrition_req == "équilibré":
            try:
                if self.recette_manager.obtenir_calories(recette_id_str_cand) > self.params['REPAS_EQUILIBRE']:
                    continue
            except Exception:
                pass
        if recette_id_str_cand in used_recipes_in_current_gen:
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
        if self.recette_manager.recette_utilise_ingredient_anti_gaspi(recette_id_str_cand):
            anti_gaspi_candidates.append(recette_id_str_cand)

    if not candidates:
        return [], {}

    if exclure_recettes_ids:
        candidates_triees = sorted(candidates, key=lambda r_id: self._get_historical_frequency(r_id))
    else:
        candidates_triees = sorted(candidates, key=lambda r_id: recettes_scores_dispo.get(r_id, -1), reverse=True)
    anti_gaspi_triees = sorted(anti_gaspi_candidates, key=lambda r_id: recettes_scores_dispo.get(r_id, -1), reverse=True)

    if anti_gaspi_triees and recettes_scores_dispo.get(anti_gaspi_triees[0], -1) >= 0.5:
        return anti_gaspi_triees[:5], recettes_ingredients_manquants

    return candidates_triees[:10], recettes_ingredients_manquants


    def generer_menu(self, mode, exclure_recettes_ids=None):
    if exclure_recettes_ids is None:
        exclure_recettes_ids = set()

    # Réinitialisation des trackers pour chaque nouvelle génération de menu
    self.recettes_selectionnees_semaine = []
    self.mots_cles_selectionnes_semaine = set()

    resultats_df_list = []
    repas_b_utilises_ids = []
    plats_transportables_semaine = {}
    used_recipes_current_generation_set = set()
    menu_recent_noms = []
    ingredients_menu_cumules = {}

    # 🔹 Correction : dictionnaire global d’utilisation d’ingrédients (ne sera pas vidé)
    ingredients_utilises_cette_semaine = {}

    if mode == 'alternatif':
        self.recette_manager.stock_simule = self.recette_manager.df_ingredients_initial.copy()

        planning_sorted = self.df_planning.sort_values("Date")
        for index, repas_planning_row in planning_sorted.iterrows():
            date_repas_dt = repas_planning_row["Date"]
            participants_str = str(repas_planning_row["Participants"])
            participants_count = self.compter_participants(participants_str)
            transportable_req = str(repas_planning_row.get("Transportable", "")).strip().lower()
            temps_req = str(repas_planning_row.get("Temps", "")).strip().lower()
            nutrition_req = str(repas_planning_row.get("Nutrition", "")).strip().lower()

            recette_choisie_id = None
            nom_plat_final = "Erreur - Plat non défini"
            remarques_repas = ""
            temps_prep_final = 0

            if participants_str == "B":
                nom_plat_final, recette_choisie_id, remarques_repas = self.generer_menu_repas_b(
                    date_repas_dt, plats_transportables_semaine, repas_b_utilises_ids, menu_recent_noms
                )
                if recette_choisie_id:
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
            else:
                # Première tentative avec contraintes pleines
                recette_choisie_id, _ = self._traiter_menu_standard(
                    date_repas_dt, participants_str, participants_count, used_recipes_current_generation_set,
                    menu_recent_noms, transportable_req, temps_req, nutrition_req,
                    exclure_recettes_ids=exclure_recettes_ids,
                    ingredients_utilises_cette_semaine=ingredients_utilises_cette_semaine  # 🔹 Passage du dict global
                )

                # Si échec → relâchement des contraintes
                if recette_choisie_id is None:
                    remarques_repas = "Aucune recette appropriée trouvée selon les critères."
                    # 1. Relâche nutrition
                    if nutrition_req == "équilibré" and not recette_choisie_id:
                        recette_choisie_id, _ = self._traiter_menu_standard(
                            date_repas_dt, participants_str, participants_count,
                            used_recipes_current_generation_set, menu_recent_noms,
                            transportable_req, temps_req, "normal",
                            exclure_recettes_ids=exclure_recettes_ids,
                            ingredients_utilises_cette_semaine=ingredients_utilises_cette_semaine
                        )
                        if recette_choisie_id:
                            remarques_repas += " Contrainte nutritionnelle relâchée."

                    # 2. Relâche temps
                    if temps_req in ["express", "rapide"] and not recette_choisie_id:
                        recette_choisie_id, _ = self._traiter_menu_standard(
                            date_repas_dt, participants_str, participants_count,
                            used_recipes_current_generation_set, menu_recent_noms,
                            transportable_req, "normal", nutrition_req,
                            exclure_recettes_ids=exclure_recettes_ids,
                            ingredients_utilises_cette_semaine=ingredients_utilises_cette_semaine
                        )
                        if recette_choisie_id:
                            remarques_repas += " Contrainte de temps relâchée."

                    # 3. Relâche transportable
                    if transportable_req == "oui" and not recette_choisie_id:
                        recette_choisie_id, _ = self._traiter_menu_standard(
                            date_repas_dt, participants_str, participants_count,
                            used_recipes_current_generation_set, menu_recent_noms,
                            "non", temps_req, nutrition_req,
                            exclure_recettes_ids=exclure_recettes_ids,
                            ingredients_utilises_cette_semaine=ingredients_utilises_cette_semaine
                        )
                        if recette_choisie_id:
                            remarques_repas += " Contrainte transportable relâchée."

                    # 4. Tout relâcher
                    if not recette_choisie_id:
                        recette_choisie_id, _ = self._traiter_menu_standard(
                            date_repas_dt, participants_str, participants_count,
                            used_recipes_current_generation_set, menu_recent_noms,
                            "non", "normal", "normal",
                            exclure_recettes_ids=exclure_recettes_ids,
                            ingredients_utilises_cette_semaine=ingredients_utilises_cette_semaine
                        )
                        if recette_choisie_id:
                            remarques_repas += " Toutes contraintes relâchées."

                # Définir le nom final
                if recette_choisie_id:
                    nom_plat_final = self.recette_manager.obtenir_nom(recette_choisie_id)
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
                    if not remarques_repas:
                        remarques_repas = "Généré automatiquement"
                else:
                    nom_plat_final = "Recette non trouvée"

            # Si une recette est choisie → MAJ trackers
            if recette_choisie_id:
                # Ajout au suivi recettes et mots-clés
                self.recettes_selectionnees_semaine.append(recette_choisie_id)
                self.mots_cles_selectionnes_semaine.add(nom_plat_final.split()[0].lower())

                # 🔹 MAJ du dict global ingrédients
                ingredients_recette_choisie = {
                    i.get("Ingrédient ok")
                    for i in self.recette_manager.get_ingredients_for_recipe(recette_choisie_id)
                    if i.get("Ingrédient ok")
                }
                for ing_id in ingredients_recette_choisie:
                    ingredients_utilises_cette_semaine[ing_id] = date_repas_dt

                # MAJ quantités pour liste de courses
                ingredients_necessaires = self.recette_manager.calculer_quantite_necessaire(recette_choisie_id, participants_count)
                for ing_id, qte_menu in ingredients_necessaires.items():
                    ingredients_menu_cumules[ing_id] = ingredients_menu_cumules.get(ing_id, 0.0) + qte_menu

                # Décrémenter le stock si nécessaire
                if not self.ne_pas_decrementer_stock:
                    self.recette_manager.decrementer_stock(recette_choisie_id, participants_count, date_repas_dt)

                # Plats transportables
                if participants_str != "B" and self.recette_manager.est_transportable(recette_choisie_id):
                    plats_transportables_semaine[date_repas_dt] = recette_choisie_id

                used_recipes_current_generation_set.add(recette_choisie_id)

            # Logging et ajout au résultat
            self._log_decision_recette(recette_choisie_id, date_repas_dt, participants_str)
            self._ajouter_resultat(resultats_df_list, date_repas_dt, nom_plat_final, participants_str,
                                   remarques_repas, temps_prep_final, recette_choisie_id)

            # Mémoriser le nom pour anti-répétition courte
            if nom_plat_final and all(x not in nom_plat_final for x in ["Pas de recette", "Pas de reste", "Erreur", "Invalide"]):
                menu_recent_noms.append(nom_plat_final)
                if len(menu_recent_noms) > 3:
                    menu_recent_noms.pop(0)

        # Création du DataFrame final + liste de courses
        df_menu_genere = pd.DataFrame(resultats_df_list)

        liste_courses_data = []
        for ing_id, qte_menu in ingredients_menu_cumules.items():
            nom_ing = self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)
            qte_stock_initial = self.recette_manager.obtenir_qte_stock_initial_par_id(ing_id)
            unite = self.recette_manager.obtenir_unite_ingredient_par_id(ing_id) or "unité(s)"
            qte_stock_simule = self.recette_manager.obtenir_qte_stock_par_id(ing_id)
            qte_acheter = max(0, qte_menu - qte_stock_initial)
            liste_courses_data.append({
                "Ingredient": f"{nom_ing} ({unite})",
                "Quantité du menu": f"{qte_menu:.2f}",
                "Qte reste (initiale)": f"{qte_stock_initial:.2f}",
                "Qte reste (simulée)": f"{qte_stock_simule:.2f}",
                "Quantité à acheter": f"{qte_acheter:.2f}"
            })

        if not df_menu_genere.empty and 'Date' in df_menu_genere.columns:
            df_menu_genere['Date'] = pd.to_datetime(df_menu_genere['Date'],
                                                    format="%d/%m/%Y %H:%M",
                                                    errors='coerce').dt.strftime('%Y-%m-%d %H:%M')
            liste_courses_data.sort(key=lambda x: x["Ingredient"])

        return df_menu_genere, liste_courses_data

    
    def _traiter_menu_standard(self, date_repas, participants_str_codes, participants_count_int, used_recipes_in_current_gen_set, menu_recent_noms_list, transportable_req_str, temps_req_str, nutrition_req_str, exclure_recettes_ids=None):
        # ... (Cette méthode reste inchangée)
        logger.debug(f"--- Traitement Repas Standard pour {date_repas.strftime('%Y-%m-%d %H:%M')} ---")
        recettes_candidates_initiales, recettes_manquants_dict = self.generer_recettes_candidates(
            date_repas, participants_str_codes, used_recipes_in_current_gen_set,
            transportable_req_str, temps_req_str, nutrition_req_str,
            exclure_recettes_ids=exclure_recettes_ids
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
                    try: mots_cles_exclus_set.add(nom_plat_recent.lower().split()[0])
                    except IndexError: pass
        if mots_cles_exclus_set:
            logger.debug(f"Mots clés exclus pour anti-répétition (génération actuelle): {mots_cles_exclus_set}")

        def get_first_word_local(recette_id_str_func):
            nom = self.recette_manager.obtenir_nom(recette_id_str_func)
            return nom.lower().split()[0] if nom and nom.strip() and "Recette_ID_" not in nom else ""

        recette_choisie_final = None
        if preferred_candidates_list:
            preferred_valides_motcle = []
            for r_id in preferred_candidates_list:
                first_word = get_first_word_local(r_id)
                if first_word not in mots_cles_exclus_set:
                    preferred_valides_motcle.append(r_id)
                else:
                    logger.debug(f"Candidat préféré {self.recette_manager.obtenir_nom(r_id)} ({r_id}) filtré: Premier mot '{first_word}' déjà récent.")

            if preferred_valides_motcle:
                recette_choisie_final = sorted(preferred_valides_motcle, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
                logger.debug(f"Recette choisie parmi les préférées valides: {self.recette_manager.obtenir_nom(recette_choisie_final)} ({recette_choisie_final}).")
            else:
                recette_choisie_final = sorted(preferred_candidates_list, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
                logger.debug(f"Recette choisie parmi les préférées (sans filtrage mot-clé, car tous sont filtrés): {self.recette_manager.obtenir_nom(recette_choisie_final)} ({recette_choisie_final}).")

        if not recette_choisie_final:
            candidates_valides_motcle = []
            for r_id in recettes_candidates_initiales:
                first_word = get_first_word_local(r_id)
                if first_word not in mots_cles_exclus_set:
                    candidates_valides_motcle.append(r_id)
                else:
                    logger.debug(f"Candidat général {self.recette_manager.obtenir_nom(r_id)} ({r_id}) filtré: Premier mot '{first_word}' déjà récent.")

            if candidates_valides_motcle:
                if exclure_recettes_ids:
                    recette_choisie_final = sorted(candidates_valides_motcle, key=lambda r_id: self._get_historical_frequency(r_id))[0]
                else:
                    recette_choisie_final = sorted(candidates_valides_motcle, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
                logger.debug(f"Recette choisie parmi les candidats généraux valides: {self.recette_manager.obtenir_nom(recette_choisie_final)} ({recette_choisie_final}).")
            elif recettes_candidates_initiales:
                if exclure_recettes_ids:
                    recette_choisie_final = sorted(recettes_candidates_initiales, key=lambda r_id: self._get_historical_frequency(r_id))[0]
                else:
                    recette_choisie_final = sorted(recettes_candidates_initiales, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
                logger.debug(f"Recette choisie parmi les candidats généraux (sans filtrage mot-clé, car tous sont filtrés): {self.recette_manager.obtenir_nom(recette_choisie_final)} ({recette_choisie_final}).")

        if recette_choisie_final:
            logger.debug(f"Recette finale sélectionnée pour repas standard: {self.recette_manager.obtenir_nom(recette_choisie_final)} ({recette_choisie_final}).")
            return recette_choisie_final, recettes_manquants_dict.get(recette_choisie_final, {})
        logger.debug(f"Aucune recette finale sélectionnée pour repas standard à {date_repas.strftime('%Y-%m-%d %H:%M')}.")
        return None, {}
    
    def _log_decision_recette(self, recette_id_str, date_repas, participants_str_codes):
        # ... (Cette méthode reste inchangée)
        if recette_id_str is not None:
            nom_recette = self.recette_manager.obtenir_nom(recette_id_str)
            adaptee = self.recette_manager.est_adaptee_aux_participants(recette_id_str, participants_str_codes)
            temps_prep = self.recette_manager.obtenir_temps_preparation(recette_id_str)
            logger.debug(f"Décision rec {recette_id_str} ({nom_recette}): Adaptée={adaptee}, Temps={temps_prep} min")
        else:
            logger.warning(f"Aucune recette sélectionnée pour {date_repas.strftime('%d/%m/%Y')} - Participants: {participants_str_codes}")

    def _ajouter_resultat(self, resultats_liste, date_repas, nom_menu_str, participants_str, remarques_str, temps_prep_int=0, recette_id_str_pour_eval=None):
        # ... (Cette méthode reste inchangée)
        info_stock_str = ""
        if recette_id_str_pour_eval:
            score_dispo, pourcentage_dispo, _ = self.recette_manager.evaluer_disponibilite_et_manquants(recette_id_str_pour_eval, self.compter_participants(participants_str))
            info_stock_str = f"Stock: {pourcentage_dispo:.0f}% des ingrédients disponibles (score: {score_dispo:.2f})"
        remarques_finales = f"{remarques_str} {info_stock_str}".strip()
        resultats_liste.append({
            "Date": date_repas.strftime("%d/%m/%Y %H:%M"),
            COLONNE_NOM: nom_menu_str,
            "Participant(s)": participants_str,
            "Remarques spécifiques": remarques_finales,
            "Temps de préparation": f"{temps_prep_int} min" if temps_prep_int else "-",
            "Recette_ID": recette_id_str_pour_eval
        })

    def generer_menu_repas_b(self, date_repas, plats_transportables_semaine_dict, repas_b_utilises_ids_list, menu_recent_noms_list):
        # ... (Cette méthode reste inchangée)
        candidats_restes_ids = []
        sorted_plats_transportables = sorted(plats_transportables_semaine_dict.items(), key=lambda item: item[0])
        logger.debug(f"--- Recherche de restes pour Repas B le {date_repas.strftime('%Y-%m-%d %H:%M')} ---")
        if not sorted_plats_transportables:
            logger.debug("Aucun plat transportable disponible dans plats_transportables_semaine_dict.")
            
        for date_plat_orig, plat_id_orig_str in sorted_plats_transportables:
            nom_plat_reste = self.recette_manager.obtenir_nom(plat_id_orig_str)
            jours_ecoules = (date_repas.date() - date_plat_orig.date()).days
            logger.debug(f"Éval reste {nom_plat_reste} (ID: {plat_id_orig_str}) du {date_plat_orig.strftime('%Y-%m-%d')}. Jours écoulés: {jours_ecoules}.")
            if not (0 < jours_ecoules <= 2):
                logger.debug(f"Reste {nom_plat_reste} filtré: Jours écoulés ({jours_ecoules}) hors de la plage (1-2 jours).")
                continue
            if plat_id_orig_str in repas_b_utilises_ids_list:
                logger.debug(f"Reste {nom_plat_reste} filtré: Déjà utilisé pour un repas B.")
                continue
            if not (nom_plat_reste and nom_plat_reste.strip() and "Recette_ID_" not in nom_plat_reste):
                logger.debug(f"Reste {nom_plat_reste} filtré: Nom de plat invalide ou générique.")
                continue
            
            if not self.recette_manager.est_transportable(plat_id_orig_str):
                logger.debug(f"Reste {nom_plat_reste} (ID: {plat_id_orig_str}) filtré: La recette d'origine n'est pas marquée comme transportable dans Recettes.csv.")
                continue
            candidats_restes_ids.append(plat_id_orig_str)
            logger.debug(f"Reste {nom_plat_reste} (ID: {plat_id_orig_str}) ajouté aux candidats restes.")
        if candidats_restes_ids:
            plat_id_choisi_str = candidats_restes_ids[0]
            nom_plat_choisi_str = self.recette_manager.obtenir_nom(plat_id_choisi_str)
            repas_b_utilises_ids_list.append(plat_id_choisi_str)
            logger.info(f"Reste choisi pour Repas B: {nom_plat_choisi_str} (ID: {plat_id_choisi_str}).")
            return f"Restes : {nom_plat_choisi_str}", plat_id_choisi_str, "Reste transportable utilisé"
        logger.info("Pas de reste disponible trouvé pour ce Repas B.")
        return "Pas de reste disponible", None, "Aucun reste transportable trouvé"

    def generer_menu(self, mode, exclure_recettes_ids=None):
        if exclure_recettes_ids is None:
            exclure_recettes_ids = set()
        
        # Réinitialisation des trackers pour chaque nouvelle génération de menu
        self.recettes_selectionnees_semaine = []
        self.mots_cles_selectionnes_semaine = set()

        resultats_df_list = []
        repas_b_utilises_ids = []
        plats_transportables_semaine = {}
        used_recipes_current_generation_set = set()
        menu_recent_noms = []
        
        ingredients_menu_cumules = {}
        
        if mode == 'alternatif':
            self.recette_manager.stock_simule = self.recette_manager.df_ingredients_initial.copy()

        initial_stock_values = {
            row[COLONNE_ID_INGREDIENT]: float(row["Qte reste"])
            for _, row in self.recette_manager.df_ingredients_initial.iterrows()
            if isinstance(row["Qte reste"], str) and row["Qte reste"].replace('.', '', 1).isdigit()
        }

        planning_sorted = self.df_planning.sort_values("Date")
        
        for index, repas_planning_row in planning_sorted.iterrows():
            date_repas_dt = repas_planning_row["Date"]
            participants_str = str(repas_planning_row["Participants"])
            participants_count = self.compter_participants(participants_str)
            transportable_req = str(repas_planning_row.get("Transportable", "")).strip().lower()
            temps_req = str(repas_planning_row.get("Temps", "")).strip().lower()
            nutrition_req = str(repas_planning_row.get("Nutrition", "")).strip().lower()

            logger.info(f"\n--- Traitement Planning: {date_repas_dt.strftime('%d/%m/%Y %H:%M')} - Participants: {participants_str} ---")

            recette_choisie_id = None
            nom_plat_final = "Erreur - Plat non défini"
            remarques_repas = ""
            temps_prep_final = 0
            
            if participants_str == "B":
                nom_plat_final, recette_choisie_id, remarques_repas = self.generer_menu_repas_b(
                    date_repas_dt, plats_transportables_semaine, repas_b_utilises_ids, menu_recent_noms
                )
                if recette_choisie_id:
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
            else:
                # Première tentative de génération avec toutes les contraintes
                recette_choisie_id, _ = self._traiter_menu_standard(
                    date_repas_dt, participants_str, participants_count, used_recipes_current_generation_set,
                    menu_recent_noms, transportable_req, temps_req, nutrition_req,
                    exclure_recettes_ids=exclure_recettes_ids
                )

                if recette_choisie_id is None:
                    # Logique de "dernier recours" si la première tentative échoue
                    logger.warning(f"Pas de recette trouvée pour {date_repas_dt.strftime('%d/%m/%Y')}. Tentative de relâcher les contraintes.")
                    
                    # Relâchement des contraintes une par une (dans un ordre de priorité inverse)
                    
                    # 1. On ignore le filtre "équilibré" si la contrainte était spécifiée
                    if nutrition_req == "équilibré":
                        logger.debug("Tentative de relâcher la contrainte nutritionnelle.")
                        recette_choisie_id, _ = self._traiter_menu_standard(
                            date_repas_dt, participants_str, participants_count, used_recipes_current_generation_set,
                            menu_recent_noms, transportable_req, temps_req, "normal",
                            exclure_recettes_ids=exclure_recettes_ids
                        )
                        if recette_choisie_id:
                            remarques_repas += "Contrainte nutritionnelle relâchée. "
                    
                    # 2. On ignore le filtre de temps si la contrainte était spécifiée
                    if not recette_choisie_id and temps_req in ["express", "rapide"]:
                        logger.debug("Tentative de relâcher la contrainte de temps.")
                        recette_choisie_id, _ = self._traiter_menu_standard(
                            date_repas_dt, participants_str, participants_count, used_recipes_current_generation_set,
                            menu_recent_noms, transportable_req, "normal", nutrition_req,
                            exclure_recettes_ids=exclure_recettes_ids
                        )
                        if recette_choisie_id:
                            remarques_repas += "Contrainte de temps relâchée. "

                    # 3. On ignore le filtre transportable si la contrainte était spécifiée
                    if not recette_choisie_id and transportable_req == "oui":
                        logger.debug("Tentative de relâcher la contrainte de transport.")
                        recette_choisie_id, _ = self._traiter_menu_standard(
                            date_repas_dt, participants_str, participants_count, used_recipes_current_generation_set,
                            menu_recent_noms, "non", temps_req, nutrition_req,
                            exclure_recettes_ids=exclure_recettes_ids
                        )
                        if recette_choisie_id:
                            remarques_repas += "Contrainte de transport relâchée. "

                    # 4. On relance le tout sans aucune contrainte spécifiquement demandée par l'utilisateur
                    if not recette_choisie_id:
                        logger.debug(f"Dernier recours: relâcher toutes les contraintes de spécificité.")
                        recette_choisie_id, _ = self._traiter_menu_standard(
                            date_repas_dt, participants_str, participants_count, used_recipes_current_generation_set,
                            menu_recent_noms, "non", "normal", "normal",
                            exclure_recettes_ids=exclure_recettes_ids
                        )
                        if recette_choisie_id:
                            remarques_repas += "Contraintes de répétition et de spécificité relâchées. "


                if recette_choisie_id:
                    nom_plat_final = self.recette_manager.obtenir_nom(recette_choisie_id)
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
                    remarques_repas = remarques_repas if remarques_repas else "Généré automatiquement"
                else:
                    nom_plat_final = "Recette non trouvée"
                    remarques_repas = "Aucune recette appropriée trouvée selon les critères, même relâchés."

            if recette_choisie_id:
                # Ajout de la recette à la liste des recettes utilisées et de son mot-clé
                self.recettes_selectionnees_semaine.append(recette_choisie_id)
                nom_recette = self.recette_manager.obtenir_nom(recette_choisie_id)
                self.mots_cles_selectionnes_semaine.add(nom_recette.split()[0].lower())

                ingredients_necessaires_ce_repas = self.recette_manager.calculer_quantite_necessaire(recette_choisie_id, participants_count)
                for ing_id, qte_menu in ingredients_necessaires_ce_repas.items():
                    current_qte = ingredients_menu_cumules.get(ing_id, 0.0)
                    ingredients_menu_cumules[ing_id] = current_qte + qte_menu
                
                if not self.ne_pas_decrementer_stock:
                    self.recette_manager.decrementer_stock(recette_choisie_id, participants_count, date_repas_dt)
                
                used_recipes_current_generation_set.add(recette_choisie_id)
                
                if participants_str != "B" and self.recette_manager.est_transportable(recette_choisie_id):
                    plats_transportables_semaine[date_repas_dt] = recette_choisie_id
                    logger.debug(f"'{nom_plat_final}' ({recette_choisie_id}) ajouté à plats_transportables_semaine pour le {date_repas_dt.strftime('%Y-%m-%d')}.")
                elif participants_str != "B":
                    logger.debug(f"'{nom_plat_final}' ({recette_choisie_id}) non ajouté à plats_transportables_semaine (transportable_req est '{transportable_req}' ou recette non transportable).")


            self._log_decision_recette(recette_choisie_id, date_repas_dt, participants_str)

            self._ajouter_resultat(
                resultats_df_list, date_repas_dt, nom_plat_final, participants_str,
                remarques_repas, temps_prep_final, recette_choisie_id
            )
            
            if nom_plat_final and "Pas de recette" not in nom_plat_final and "Pas de reste" not in nom_plat_final and "Erreur" not in nom_plat_final and "Invalide" not in nom_plat_final:
                menu_recent_noms.append(nom_plat_final)
                if len(menu_recent_noms) > 3:
                    menu_recent_noms.pop(0)


        df_menu_genere = pd.DataFrame(resultats_df_list)

        liste_courses_data = []
        for ing_id, qte_menu in ingredients_menu_cumules.items():
            nom_ing = self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)
            qte_stock_initial = self.recette_manager.obtenir_qte_stock_initial_par_id(ing_id)
            unite = self.recette_manager.obtenir_unite_ingredient_par_id(ing_id) or "unité(s)"
            qte_stock_simule = self.recette_manager.obtenir_qte_stock_par_id(ing_id)
            qte_acheter = max(0, qte_menu - qte_stock_initial)

            liste_courses_data.append({
                "Ingredient": f"{nom_ing} ({unite})",
                "Quantité du menu": f"{qte_menu:.2f}",
                "Qte reste (initiale)": f"{qte_stock_initial:.2f}",
                "Qte reste (simulée)": f"{qte_stock_simule:.2f}",
                "Quantité à acheter": f"{qte_acheter:.2f}"
            })

        if not df_menu_genere.empty:
            logger.info(f"Nombre de lignes totales générées : {len(df_menu_genere)}")
            if 'Date' in df_menu_genere.columns:
                df_menu_genere['Date'] = pd.to_datetime(df_menu_genere['Date'], format="%d/%m/%Y %H:%M", errors='coerce').dt.strftime('%Y-%m-%d %H:%M')
        
        liste_courses_data.sort(key=lambda x: x["Ingredient"])

        return df_menu_genere, liste_courses_data

# Nouvelle fonction pour envoyer les données à Notion
def add_menu_to_notion(df_menu, notion_db_id):
    # ... (Cette méthode reste inchangée)
    success_count = 0
    failure_count = 0
    
    # ID de la page 'Courses' pour la relation
    COURSES_PAGE_ID = "1c66fa46f8b2809ca9b7c11ffaf1d582"

    for _, row in df_menu.iterrows():
        recette_id = row.get('Recette_ID')
        nom_plat = row.get(COLONNE_NOM)
        participants = row.get('Participant(s)')
        date_str = row.get('Date')
        
        if not date_str:
            st.warning(f"Date invalide pour la ligne : {nom_plat}. L'enregistrement sera ignoré.")
            failure_count += 1
            continue
        
        try:
            # Ici on parse date_str complet avec datetime, en considérant qu'il contient heure (ex: '2025-08-07 08:00')
            dt = datetime.strptime(date_str, '%Y-%m-%d %H:%M')
            # On convertit en isoformat complet
            date_notion = dt.isoformat()  # Ex: '2025-08-07T08:00:00'
        except ValueError:
            st.warning(f"Date invalide pour la ligne : {date_str}. L'enregistrement sera ignoré.")
            failure_count += 1
            continue
        
        
        # Le dictionnaire des propriétés de la page
        new_page_properties = {
            "Nom Menu": {
                "title": [
                    {
                        "text": {
                            "content": nom_plat
                        }
                    }
                ]
            },
            "Date": {
                "date": {
                    "start": date_notion
                }
            },
            "Liste": {
                "relation": [
                    {"id": COURSES_PAGE_ID}
                ]
            }
        }
        
        # Ajout de la relation de recette UNIQUEMENT si l'ID est disponible et que ce n'est pas un repas "Restes"
        if recette_id and "Restes" not in str(nom_plat):
            new_page_properties["Recette"] = {
                "relation": [
                    {"id": recette_id}
                ]
            }

        # Ajout des participants UNIQUEMENT si la valeur est disponible
        if participants and isinstance(participants, str):
            participants_list = [p.strip() for p in participants.split(',') if p.strip()]
            if participants_list:
                new_page_properties["Participant(s)"] = {
                    "multi_select": [
                        {"name": p} for p in participants_list
                    ]
                }

        try:
            notion.pages.create(
                parent={"database_id": notion_db_id},
                properties=new_page_properties
            )
            success_count += 1
        except Exception as e:
            logger.error(f"Erreur lors de l'envoi de la ligne '{nom_plat}' à Notion : {e}")
            failure_count += 1
            
    return success_count, failure_count

# --- Streamlit UI ---

@st.cache_data(show_spinner=False)
def load_notion_data(saison_filtre_selection):
    """
    Charge les données de Notion. Utilise le cache Streamlit pour ne pas recharger.
    """
    st.sidebar.info("Chargement des données depuis Notion en cours...")
    
    with st.spinner("Chargement des Menus..."):
        df_menus = extract_menus()
    st.sidebar.success("✅ Menus chargés.")

    with st.spinner(f"Chargement des Recettes pour la saison '{saison_filtre_selection}'..."):
        df_recettes = extract_recettes(saison_filtre_selection)
    st.sidebar.success("✅ Recettes chargées.")

    with st.spinner("Chargement des Ingrédients..."):
        df_ingredients = extract_ingredients()
    st.sidebar.success("✅ Ingrédients chargés.")

    with st.spinner("Chargement des Ingrédients-Recettes..."):
        df_ingredients_recettes = extract_ingr_rec()
    st.sidebar.success("✅ Ingrédients-Recettes chargés.")

    st.sidebar.success("Toutes les données de Notion sont prêtes.")
    
    return {
        "Menus": df_menus,
        "Recettes": df_recettes,
        "Ingredients": df_ingredients,
        "Ingredients_recettes": df_ingredients_recettes
    }

def main():
    st.set_page_config(layout="wide", page_title="Générateur de Menus et Liste de Courses")
    st.title("🍽️ Générateur de Menus et Liste de Courses")
    st.markdown("---")
    
    # --- Paramètres de génération (section dépliante) ---
    with st.sidebar.expander("⚙️ Paramètres de génération"):
        # Initialisation des valeurs par défaut
        if 'NB_JOURS_ANTI_REPETITION' not in st.session_state:
            st.session_state['NB_JOURS_ANTI_REPETITION'] = NB_JOURS_ANTI_REPETITION_DEFAULT
        if 'REPAS_EQUILIBRE' not in st.session_state:
            st.session_state['REPAS_EQUILIBRE'] = REPAS_EQUILIBRE_DEFAULT
        if 'TEMPS_MAX_EXPRESS' not in st.session_state:
            st.session_state['TEMPS_MAX_EXPRESS'] = TEMPS_MAX_EXPRESS_DEFAULT
        if 'TEMPS_MAX_RAPIDE' not in st.session_state:
            st.session_state['TEMPS_MAX_RAPIDE'] = TEMPS_MAX_RAPIDE_DEFAULT

        # Inputs pour les paramètres
        st.session_state['NB_JOURS_ANTI_REPETITION'] = st.number_input(
            "Délai entre menus identiques (jours)", 
            min_value=1, 
            max_value=365, 
            value=st.session_state['NB_JOURS_ANTI_REPETITION'],
            key="input_jours_anti_repetition"
        )
        st.session_state['REPAS_EQUILIBRE'] = st.number_input(
            "Calories max pour repas 'équilibré'", 
            min_value=100, 
            max_value=2000, 
            step=50, 
            value=st.session_state['REPAS_EQUILIBRE'],
            key="input_repas_equilibre"
        )
        st.session_state['TEMPS_MAX_EXPRESS'] = st.number_input(
            "Temps max pour repas 'express' (min)", 
            min_value=5, 
            max_value=60, 
            step=5, 
            value=st.session_state['TEMPS_MAX_EXPRESS'],
            key="input_temps_express"
        )
        st.session_state['TEMPS_MAX_RAPIDE'] = st.number_input(
            "Temps max pour repas 'rapide' (min)", 
            min_value=5, 
            max_value=90, 
            step=5, 
            value=st.session_state['TEMPS_MAX_RAPIDE'],
            key="input_temps_rapide"
        )

        saison_actuelle = get_current_season()
        saisons_disponibles = ["Printemps", "Été", "Automne", "Hiver"]
        try:
            index_saison_defaut = saisons_disponibles.index(saison_actuelle)
        except ValueError:
            index_saison_defaut = 0
            
        saison_selectionnee = st.selectbox(
            "Sélectionnez la saison:",
            options=saisons_disponibles,
            index=index_saison_defaut,
            key="saison_filtre"
        )

    st.sidebar.header("Fichiers de données")
    
    
    uploaded_files = {}
    uploaded_files["Planning.csv"] = st.sidebar.file_uploader(
        "Uploader Planning.csv (votre planning de repas)", 
        type="csv", 
        key="Planning.csv"
    )

    if uploaded_files["Planning.csv"] is None:
        st.warning("Veuillez charger le fichier CSV de planning pour continuer.")
        return

    dataframes = {}

    try:
        uploaded_files["Planning.csv"].seek(0)
        df_planning = pd.read_csv(
            uploaded_files["Planning.csv"],
            encoding='utf-8',
            sep=';',
            parse_dates=['Date'],
            dayfirst=True
        )
        dataframes["Planning"] = df_planning
        st.sidebar.success("Planning.csv chargé avec succès.")
    except Exception as e:
        st.sidebar.error(f"Erreur lors du chargement de Planning.csv: {e}")
        return

    if 'generation_reussie' not in st.session_state:
        st.session_state['generation_reussie'] = False
    if 'df_menu_realiste' not in st.session_state:
        st.session_state['df_menu_realiste'] = pd.DataFrame()
    if 'df_menu_alternatif' not in st.session_state:
        st.session_state['df_menu_alternatif'] = pd.DataFrame()
    if 'liste_courses_realiste' not in st.session_state:
        st.session_state['liste_courses_realiste'] = []
    if 'liste_courses_alternatif' not in st.session_state:
        st.session_state['liste_courses_alternatif'] = []

    st.markdown("---")
    st.header("1. Générer et Exporter en 1 clic")
    st.write("Ce bouton charge les données, génère le menu Optimal et l'envoie à Notion. Il génère aussi un menu alternatif.")
    
    if st.button("🚀 Générer et Envoyer le Menu Optimal (1 clic)", use_container_width=True):
        st.session_state['generation_reussie'] = False
        
        saison_selectionnee = st.session_state.get("saison_filtre", get_current_season())

        with st.spinner("Chargement des données Notion..."):
            try:
                notion_data = load_notion_data(saison_selectionnee)
                dataframes.update(notion_data)
            except Exception as e:
                st.error(f"Erreur lors de la récupération des données depuis Notion : {e}")
                return
        
        with st.spinner("Vérification des colonnes..."):
            try:
                verifier_colonnes(dataframes["Recettes"], [COLONNE_ID_RECETTE, COLONNE_NOM, COLONNE_TEMPS_TOTAL, COLONNE_AIME_PAS_PRINCIP, "Transportable", "Calories", "Proteines"], "Recettes")
                verifier_colonnes(dataframes["Planning"], ["Date", "Participants", "Transportable", "Temps", "Nutrition"], "Planning.csv")
                verifier_colonnes(dataframes["Menus"], ["Date", "Recette"], "Menus")
                verifier_colonnes(dataframes["Ingredients"], [COLONNE_ID_INGREDIENT, "Nom", "Qte reste", "unité", "Intervalle"], "Ingredients")
                verifier_colonnes(dataframes["Ingredients_recettes"], [COLONNE_ID_RECETTE, "Ingrédient ok", "Qté/pers_s"], "Ingredients_recettes")
            except ValueError as ve:
                st.error(f"Erreur de données : {ve}")
                return

        with st.spinner("Génération du menu Optimal et alternatif..."):
            try:
                params = {
                    "NB_JOURS_ANTI_REPETITION": st.session_state['NB_JOURS_ANTI_REPETITION'],
                    "REPAS_EQUILIBRE": st.session_state['REPAS_EQUILIBRE'],
                    "TEMPS_MAX_EXPRESS": st.session_state['TEMPS_MAX_EXPRESS'],
                    "TEMPS_MAX_RAPIDE": st.session_state['TEMPS_MAX_RAPIDE']
                }

                menu_generator_realiste = MenuGenerator(
                    dataframes["Menus"],
                    dataframes["Recettes"],
                    dataframes["Planning"],
                    dataframes["Ingredients"],
                    dataframes["Ingredients_recettes"],
                    ne_pas_decrementer_stock=False,
                    params=params
                )
                df_menu_realiste, liste_courses_realiste = menu_generator_realiste.generer_menu(mode='realiste')
                st.session_state['df_menu_realiste'] = df_menu_realiste
                st.session_state['liste_courses_realiste'] = liste_courses_realiste

                recettes_a_exclure = set(df_menu_realiste[df_menu_realiste['Recette_ID'].notna()]['Recette_ID'].astype(str).tolist())

                menu_generator_alternatif = MenuGenerator(
                    dataframes["Menus"],
                    dataframes["Recettes"],
                    dataframes["Planning"],
                    dataframes["Ingredients"],
                    dataframes["Ingredients_recettes"],
                    ne_pas_decrementer_stock=True,
                    params=params
                )
                df_menu_alternatif, liste_courses_alternatif = menu_generator_alternatif.generer_menu(mode='alternatif', exclure_recettes_ids=recettes_a_exclure)
                st.session_state['df_menu_alternatif'] = df_menu_alternatif
                st.session_state['liste_courses_alternatif'] = liste_courses_alternatif
                
            except Exception as e:
                st.error(f"Une erreur est survenue lors de la génération du menu : {e}")
                return

        with st.spinner("Envoi du menu à Notion..."):
            success, failure = add_menu_to_notion(st.session_state['df_menu_realiste'], ID_MENUS)
            if success > 0:
                st.success(f"✅ Opération '1 clic' réussie ! {success} repas ont été ajoutés à votre base de données Notion 'Menus' !")
            if failure > 0:
                st.warning(f"⚠️ {failure} repas n'ont pas pu être ajoutés (voir les logs pour plus de détails).")
            if success == 0 and failure == 0:
                st.info("Aucun repas valide à ajouter.")

        st.session_state['generation_reussie'] = True

    st.markdown("---")
    st.header("2. Générer les Menus")
    st.write("Cliquez sur le bouton ci-dessous pour générer les deux versions du menu hebdomadaire et leurs listes de courses.")
    
    if st.button("🚀 Générer 2 Menus (Optimal & Alternatif)"):
        st.session_state['generation_reussie'] = False
        
        saison_selectionnee = st.session_state.get("saison_filtre", get_current_season())

        with st.spinner("Chargement des données Notion..."):
            try:
                notion_data = load_notion_data(saison_selectionnee)
                dataframes.update(notion_data)
            except Exception as e:
                st.error(f"Erreur lors de la récupération des données depuis Notion : {e}")
                return

        with st.spinner("Vérification des colonnes..."):
            try:
                verifier_colonnes(dataframes["Recettes"], [COLONNE_ID_RECETTE, COLONNE_NOM, COLONNE_TEMPS_TOTAL, COLONNE_AIME_PAS_PRINCIP, "Transportable", "Calories", "Proteines"], "Recettes")
                verifier_colonnes(dataframes["Planning"], ["Date", "Participants", "Transportable", "Temps", "Nutrition"], "Planning.csv")
                verifier_colonnes(dataframes["Menus"], ["Date", "Recette"], "Menus")
                verifier_colonnes(dataframes["Ingredients"], [COLONNE_ID_INGREDIENT, "Nom", "Qte reste", "unité", "Intervalle"], "Ingredients")
                verifier_colonnes(dataframes["Ingredients_recettes"], [COLONNE_ID_RECETTE, "Ingrédient ok", "Qté/pers_s"], "Ingredients_recettes")
            except ValueError as ve:
                st.error(f"Erreur de données : {ve}")
                return

        with st.spinner("Génération des deux menus en cours..."):
            try:
                params = {
                    "NB_JOURS_ANTI_REPETITION": st.session_state['NB_JOURS_ANTI_REPETITION'],
                    "REPAS_EQUILIBRE": st.session_state['REPAS_EQUILIBRE'],
                    "TEMPS_MAX_EXPRESS": st.session_state['TEMPS_MAX_EXPRESS'],
                    "TEMPS_MAX_RAPIDE": st.session_state['TEMPS_MAX_RAPIDE']
                }

                menu_generator_realiste = MenuGenerator(
                    dataframes["Menus"],
                    dataframes["Recettes"],
                    dataframes["Planning"],
                    dataframes["Ingredients"],
                    dataframes["Ingredients_recettes"],
                    ne_pas_decrementer_stock=False,
                    params=params
                )
                df_menu_realiste, liste_courses_realiste = menu_generator_realiste.generer_menu(mode='realiste')
                st.session_state['df_menu_realiste'] = df_menu_realiste
                st.session_state['liste_courses_realiste'] = liste_courses_realiste

                recettes_a_exclure = set(df_menu_realiste[df_menu_realiste['Recette_ID'].notna()]['Recette_ID'].astype(str).tolist())

                menu_generator_alternatif = MenuGenerator(
                    dataframes["Menus"],
                    dataframes["Recettes"],
                    dataframes["Planning"],
                    dataframes["Ingredients"],
                    dataframes["Ingredients_recettes"],
                    ne_pas_decrementer_stock=True,
                    params=params
                )
                df_menu_alternatif, liste_courses_alternatif = menu_generator_alternatif.generer_menu(mode='alternatif', exclure_recettes_ids=recettes_a_exclure)
                st.session_state['df_menu_alternatif'] = df_menu_alternatif
                st.session_state['liste_courses_alternatif'] = liste_courses_alternatif
                
            except Exception as e:
                st.error(f"Une erreur est survenue lors de la génération du menu : {e}")
                return

        st.session_state['generation_reussie'] = True


    # --- Affichage des résultats ---
    if st.session_state.get('generation_reussie'):
        st.header("Menus et Listes de Courses générés")
        
        tab_optimal, tab_alternatif = st.tabs(["Menu Optimal (avec stock)", "Menu Alternatif"])
        
        with tab_optimal:
            st.subheader("Menu Optimal")
            st.write("Ce menu a été généré en tenant compte de votre stock pour minimiser le nombre d'ingrédients à acheter.")
            df_menu_optimal = st.session_state['df_menu_realiste']
            df_menu_optimal_display = df_menu_optimal.drop(columns=['Recette_ID'])
            st.dataframe(df_menu_optimal_display, use_container_width=True)

            csv_data_optimal = df_menu_optimal.to_csv(index=False, sep=';', encoding='utf-8-sig')
            st.download_button(
                label="📥 Télécharger le menu OPTIMAL en CSV",
                data=csv_data_optimal,
                file_name="menu_optimal.csv",
                mime="text/csv",
                use_container_width=True
            )
            
            st.subheader("Liste de Courses Détaillée pour le Menu Optimal")
            if st.session_state['liste_courses_realiste']:
                liste_courses_df_optimal = pd.DataFrame(st.session_state['liste_courses_realiste'])
                st.dataframe(liste_courses_df_optimal, use_container_width=True)
                csv_optimal = liste_courses_df_optimal.to_csv(index=False, sep=';', encoding='utf-8-sig')
                st.download_button(
                    label="Télécharger la liste de courses OPTIMALE (CSV)",
                    data=csv_optimal,
                    file_name="liste_courses_optimale.csv",
                    mime="text/csv",
                    use_container_width=True
                )
            else:
                st.info("Aucun ingrédient manquant identifié pour la liste de courses optimale.")

        with tab_alternatif:
            st.subheader("Menu Alternatif")
            st.write("Ce menu a été généré sans tenir compte de votre stock. Il ne contient aucune recette utilisée dans le menu Optimal.")
            df_menu_alternatif = st.session_state['df_menu_alternatif']
            if not df_menu_alternatif.empty:
                df_menu_alternatif_display = df_menu_alternatif.drop(columns=['Recette_ID'])
                st.dataframe(df_menu_alternatif_display, use_container_width=True)

                csv_data_alternatif = df_menu_alternatif.to_csv(index=False, sep=';', encoding='utf-8-sig')
                st.download_button(
                    label="📥 Télécharger le menu ALTERNATIF en CSV",
                    data=csv_data_alternatif,
                    file_name="menu_alternatif.csv",
                    mime="text/csv",
                    use_container_width=True
                )
            else:
                st.info("Le menu alternatif n'a pas pu être généré. Soit il n'y a pas assez de recettes, soit toutes les recettes valides ont déjà été utilisées dans le menu optimal.")
                st.download_button(
                    label="📥 Télécharger le menu ALTERNATIF en CSV",
                    data=";;",
                    file_name="menu_alternatif.csv",
                    mime="text/csv",
                    disabled=True,
                    use_container_width=True
                )
            
            st.subheader("Liste de Courses Détaillée pour le Menu Alternatif")
            if 'liste_courses_alternatif' in st.session_state and st.session_state['liste_courses_alternatif']:
                liste_courses_df_alternatif = pd.DataFrame(st.session_state['liste_courses_alternatif'])
                st.dataframe(liste_courses_df_alternatif, use_container_width=True)
                csv_alternatif = liste_courses_df_alternatif.to_csv(index=False, sep=';', encodfing='utf-8-sig')
                st.download_button(
                    label="Télécharger la liste de courses ALTERNATIVE (CSV)",
                    data=csv_alternatif,
                    file_name="liste_courses_alternatif.csv",
                    mime="text/csv",
                    use_container_width=True
                )
            else:
                st.info("Aucun ingrédient manquant identifié pour la liste de courses alternative.")


if __name__ == "__main__":
    main()
