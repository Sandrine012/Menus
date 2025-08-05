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
NOTION_API_KEY           = st.secrets["notion_api_key"]
ID_RECETTES              = st.secrets["notion_database_id_recettes"]
ID_MENUS                 = st.secrets["notion_database_id_menus"]
ID_INGREDIENTS           = st.secrets["notion_database_id_ingredients"]
ID_INGREDIENTS_RECETTES  = st.secrets["notion_database_id_ingredients_recettes"]
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

HDR_INGR = ["Page_ID","Nom","Type de stock","unité","Qte reste"]
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
        rows.append([
            p["id"],
            "".join(t["plain_text"] for t in pr["Nom"]["title"]),
            (pr["Type de stock"]["select"] or {}).get("name",""),
            unite,
            str(qte or "")
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

        self.df_ingredients_recettes = df_ingredients_recettes.copy()

        self.stock_initial = df_ingredients.copy()
        if "Qte reste" in self.stock_initial.columns:
            self.stock_initial["Qte reste"] = pd.to_numeric(self.stock_initial["Qte reste"], errors='coerce').fillna(0.0).astype(float)
        else:
            logger.error("'Qte reste' manquante dans df_ingredients pour stock_initial.")
            self.stock_initial["Qte reste"] = 0.0

        self.stock_simule = self.stock_initial.copy()
        self.stock_initial = self.stock_initial.set_index(COLONNE_ID_INGREDIENT)
        self.stock_simule = self.stock_simule.set_index(COLONNE_ID_INGREDIENT)

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
        if not all(col in self.stock_simule.columns for col in ["Qte reste", "unité", "Nom"]):
            logger.warning("Colonnes manquantes dans stock_simule pour _trouver_ingredients_stock_eleve.")
            return {}

        for ing_id, row in self.stock_simule.iterrows():
            try:
                qte = float(str(row["Qte reste"]).replace(",", "."))
                unite = str(row["unité"]).lower()
                if (unite in ["gr", "g", "ml", "cl"] and qte >= seuil_gr) or \
                   (unite in ["pc", "tranches"] and qte >= seuil_pc):
                    ingredients_stock[ing_id] = row["Nom"]
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
            if ing_id_str not in self.stock_simule.index:
                qte_en_stock = 0.0
                logger.debug(f"Ingrédient {ing_id_str} (recette {recette_id_str}) non trouvé dans stock_simule.")
            else:
                try:
                    qte_en_stock = float(self.stock_simule.loc[ing_id_str, "Qte reste"])
                except (ValueError, IndexError, KeyError) as e:
                    logger.error(f"Erreur lecture stock pour {ing_id_str} rec {recette_id_str}: {e}")
                    qte_en_stock = 0.0

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

    def decrementer_stock(self, recette_id_str, nb_personnes, date_repas):
        ingredients_necessaires = self.calculer_quantite_necessaire(recette_id_str, nb_personnes)
        ingredients_consommes_ids = set()

        for ing_id, qte_necessaire in ingredients_necessaires.items():
            ing_id_str = str(ing_id)
            if ing_id_str not in self.stock_simule.index:
                logger.debug(f"Ingrédient {ing_id_str} (recette {recette_id_str}) non trouvé dans stock_simule pour décrémentation.")
                continue

            try:
                qte_actuelle = float(self.stock_simule.loc[ing_id_str, "Qte reste"])
                if qte_actuelle > 0 and qte_necessaire > 0:
                    qte_a_consommer = min(qte_actuelle, qte_necessaire)
                    nouvelle_qte = qte_actuelle - qte_a_consommer
                    self.stock_simule.loc[ing_id_str, "Qte reste"] = nouvelle_qte

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
            return self.df_recettes.loc[recette_page_id_str, COLONNE_NOM]
        except (KeyError, IndexError):
            logger.warning(f"Recette ID {recette_page_id_str} non trouvé dans df_recettes (obtenir_nom).")
            return f"Recette_ID_{recette_page_id_str}"
        except Exception as e:
            logger.error(f"Erreur obtenir_nom pour recette ID {recette_page_id_str}: {e}")
            return None

    def obtenir_nom_ingredient_par_id(self, ing_page_id_str):
        try:
            ing_page_id_str = str(ing_page_id_str)
            if ing_page_id_str in self.stock_initial.index:
                return self.stock_initial.loc[ing_page_id_str, 'Nom']
            else:
                logger.warning(f"Nom introuvable pour ingrédient ID: {ing_page_id_str} dans stock_initial.")
                return f"ID_Ing_{ing_page_id_str}"
        except Exception as e:
            logger.error(f"Erreur obtenir_nom_ingredient_par_id pour {ing_page_id_str}: {e}")
            return None
    
    def obtenir_unite_ingredient_par_id(self, ing_page_id_str):
        try:
            ing_page_id_str = str(ing_page_id_str)
            if ing_page_id_str in self.stock_initial.index:
                return self.stock_initial.loc[ing_page_id_str, 'unité']
            else:
                logger.warning(f"Unité introuvable pour ingrédient ID: {ing_page_id_str} dans stock_initial.")
                return None
        except Exception as e:
            logger.error(f"Erreur obtenir_unite_ingredient_par_id pour {ing_page_id_str}: {e}")
            return None

    def obtenir_qte_stock_par_id(self, ing_page_id_str):
        try:
            ing_page_id_str = str(ing_page_id_str)
            qte_stock = self.stock_initial.loc[ing_page_id_str, 'Qte reste']
            return float(qte_stock)
        except (IndexError, KeyError, ValueError):
            return 0.0
        except Exception as e:
            logger.error(f"Erreur obtenir_qte_stock_par_id pour {ing_page_id_str}: {e}")
            return 0.0

    def est_adaptee_aux_participants(self, recette_page_id_str, participants_str_codes):
        try:
            recette_page_id_str = str(recette_page_id_str)
            recette_info = self.df_recettes.loc[recette_page_id_str]
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
            valeur = str(self.df_recettes.loc[recette_page_id_str, "Transportable"]).strip().lower()
            
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
            recette_info = self.df_recettes.loc[recette_page_id_str]
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

class MenusHistoryManager:
    """Gère l'accès et les opérations sur l'historique des menus."""
    def __init__(self, df_menus_hist):
        self.df_menus_historique = df_menus_hist.copy()
        self.df_menus_historique["Date"] = pd.to_datetime(self.df_menus_historique["Date"], errors="coerce")
        self.df_menus_historique.dropna(subset=["Date"], inplace=True)
        if 'Date' in self.df_menus_historique.columns:
            self.df_menus_historique['Semaine'] = self.df_menus_historique['Date'].dt.isocalendar().week
        else:
            logger.warning("La colonne 'Date' est manquante dans l'historique des menus, impossible de calculer la semaine.")

class MenuGenerator:
    """Génère les menus en fonction du planning et des règles."""
    def __init__(self, df_menus_hist, df_recettes, df_planning, df_ingredients, df_ingredients_recettes):
        self.df_planning = df_planning.copy()
        if "Date" in self.df_planning.columns:
            self.df_planning['Date'] = pd.to_datetime(self.df_planning['Date'], errors='coerce')
            self.df_planning.dropna(subset=['Date'], inplace=True)
        else:
            logger.error("'Date' manquante dans le planning.")
            raise ValueError("Colonne 'Date' manquante dans le fichier de planning.")

        self.recette_manager = RecetteManager(df_recettes, df_ingredients, df_ingredients_recettes)
        self.menus_history_manager = MenusHistoryManager(df_menus_hist)

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

            debut = date_actuelle - timedelta(days=NB_JOURS_ANTI_REPETITION)
            fin = date_actuelle + timedelta(days=NB_JOURS_ANTI_REPETITION)
            mask = (
                (df_hist['Recette'].astype(str) == str(recette_page_id_str)) &
                (df_hist['Date'] >= debut) &
                (df_hist['Date'] <= fin)
            )
            is_recent = not df_hist.loc[mask].empty
            if is_recent:
                logger.debug(f"Recette {self.recette_manager.obtenir_nom(recette_page_id_str)} ({recette_page_id_str}) filtrée: Est récente (dans les {NB_JOURS_ANTI_REPETITION} jours)")
            return is_recent

        except Exception as e:
            logger.error(f"Erreur est_recente pour {recette_page_id_str} à {date_actuelle}: {e}")
            return False

    def compter_participants(self, participants_str_codes):
        if not isinstance(participants_str_codes, str): return 1
        if participants_str_codes == "B": return 1
        return len([p for p in participants_str_codes.replace(" ", "").split(",") if p])

    def _filtrer_recette_base(self, recette_id_str, participants_str_codes):
        return self.recette_manager.est_adaptee_aux_participants(recette_id_str, participants_str_codes)

    def generer_recettes_candidates(self, date_repas, participants_str_codes, used_recipes_in_current_gen, transportable_req, temps_req, nutrition_req):
        candidates = []
        anti_gaspi_candidates = []
        recettes_scores_dispo = {}
        recettes_ingredients_manquants = {}

        nb_personnes = self.compter_participants(participants_str_codes)

        logger.debug(f"--- Recherche de candidats pour {date_repas.strftime('%Y-%m-%d %H:%M')} (Participants: {participants_str_codes}) ---")

        for recette_id_str_cand in self.recette_manager.df_recettes.index.astype(str):
            nom_recette_cand = self.recette_manager.obtenir_nom(recette_id_str_cand)

            if str(transportable_req).strip().lower() == "oui" and not self.recette_manager.est_transportable(recette_id_str_cand):
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Non transportable pour une demande transportable.")
                continue

            temps_total = self.recette_manager.obtenir_temps_preparation(recette_id_str_cand)
            if temps_req == "express" and temps_total > TEMPS_MAX_EXPRESS:
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Temps ({temps_total} min) > Express ({TEMPS_MAX_EXPRESS} min).")
                continue
            if temps_req == "rapide" and temps_total > TEMPS_MAX_RAPIDE:
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Temps ({temps_total} min) > Rapide ({TEMPS_MAX_RAPIDE} min).")
                continue

            if recette_id_str_cand in used_recipes_in_current_gen:
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Déjà utilisé dans la génération actuelle.")
                continue
            
            if not self._filtrer_recette_base(recette_id_str_cand, participants_str_codes):
                continue
            
            if self.est_recente(recette_id_str_cand, date_repas):
                continue

            if nutrition_req == "equilibré":
                try:
                    calories = float(self.recette_manager.df_recettes.loc[recette_id_str_cand, "Calories"])
                    if calories > REPAS_EQUILIBRE:
                        logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Calories ({calories}) > Équilibré ({REPAS_EQUILIBRE}).")
                        continue
                except (KeyError, ValueError, TypeError, IndexError):
                    logger.debug(f"Calories non valides/trouvées pour {nom_recette_cand} ({recette_id_str_cand}) (filtre nutrition).")
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

        candidates_triees = sorted(candidates, key=lambda r_id: recettes_scores_dispo.get(r_id, -1), reverse=True)
        anti_gaspi_triees = sorted(anti_gaspi_candidates, key=lambda r_id: recettes_scores_dispo.get(r_id, -1), reverse=True)

        if anti_gaspi_triees and recettes_scores_dispo.get(anti_gaspi_triees[0], -1) >= 0.5:
            logger.debug(f"Priorisation des candidats anti-gaspi (meilleur score {recettes_scores_dispo.get(anti_gaspi_triees[0], -1):.2f}).")
            return anti_gaspi_triees[:5], recettes_ingredients_manquants
        
        logger.debug(f"Retourne les {min(len(candidates_triees), 10)} meilleurs candidats généraux.")
        return candidates_triees[:10], recettes_ingredients_manquants

    def _traiter_menu_standard(self, date_repas, participants_str_codes, participants_count_int, used_recipes_in_current_gen_set, menu_recent_noms_list, transportable_req_str, temps_req_str, nutrition_req_str):
        logger.debug(f"--- Traitement Repas Standard pour {date_repas.strftime('%Y-%m-%d %H:%M')} ---")
        recettes_candidates_initiales, recettes_manquants_dict = self.generer_recettes_candidates(
            date_repas, participants_str_codes, used_recipes_in_current_gen_set,
            transportable_req_str, temps_req_str, nutrition_req_str
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
                recette_choisie_final = sorted(candidates_valides_motcle, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
                logger.debug(f"Recette choisie parmi les candidats généraux valides: {self.recette_manager.obtenir_nom(recette_choisie_final)} ({recette_choisie_final}).")
            elif recettes_candidates_initiales:
                recette_choisie_final = sorted(recettes_candidates_initiales, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
                logger.debug(f"Recette choisie parmi les candidats généraux (sans filtrage mot-clé, car tous sont filtrés): {self.recette_manager.obtenir_nom(recette_choisie_final)} ({recette_choisie_final}).")

        if recette_choisie_final:
            logger.debug(f"Recette finale sélectionnée pour repas standard: {self.recette_manager.obtenir_nom(recette_choisie_final)} ({recette_choisie_final}).")
            return recette_choisie_final, recettes_manquants_dict.get(recette_choisie_final, {})
        logger.debug(f"Aucune recette finale sélectionnée pour repas standard à {date_repas.strftime('%Y-%m-%d %H:%M')}.")
        return None, {}

    def _log_decision_recette(self, recette_id_str, date_repas, participants_str_codes):
        if recette_id_str is not None:
            nom_recette = self.recette_manager.obtenir_nom(recette_id_str)
            adaptee = self.recette_manager.est_adaptee_aux_participants(recette_id_str, participants_str_codes)
            temps_prep = self.recette_manager.obtenir_temps_preparation(recette_id_str)
            logger.debug(f"Décision rec {recette_id_str} ({nom_recette}): Adaptée={adaptee}, Temps={temps_prep} min")
        else:
            logger.warning(f"Aucune recette sélectionnée pour {date_repas.strftime('%d/%m/%Y')} - Participants: {participants_str_codes}")

    def _ajouter_resultat(self, resultats_liste, date_repas, nom_menu_str, participants_str_codes, remarques_str, temps_prep_int=0, recette_id_str_pour_eval=None):
        info_stock_str = ""
        if recette_id_str_pour_eval:
            score_dispo, pourcentage_dispo, _ = self.recette_manager.evaluer_disponibilite_et_manquants(recette_id_str_pour_eval, self.compter_participants(participants_str_codes))
            info_stock_str = f"Stock: {pourcentage_dispo:.0f}% des ingrédients disponibles (score: {score_dispo:.2f})"

        remarques_finales = f"{remarques_str} {info_stock_str}".strip()
        resultats_liste.append({
            "Date": date_repas.strftime("%d/%m/%Y"),
            "Repas": nom_menu_str,
            "Participant(s)": participants_str_codes,
            "Temps total (min)": temps_prep_int,
            "Recette_ID": recette_id_str_pour_eval,
            "Remarques": remarques_finales
        })


    def generer_menu(self):
        resultats_menu = []
        ingredients_menu_cumules = {}
        used_recipes_in_current_gen = set()
        menu_recent_noms = []
        
        for _, row in self.df_planning.iterrows():
            date_repas, nom_repas, participants_str, transportable_req, temps_req, nutrition_req = (
                row["Date"], row["Repas"], str(row["Participant(s)"]), str(row["Transportable"]),
                str(row["Temps de préparation"]), str(row["Nutrition"])
            )
            
            participants_count_int = self.compter_participants(participants_str)

            recette_choisie_id = None
            manquants_recette = {}

            if "Recette" in row and pd.notna(row["Recette"]) and str(row["Recette"]).strip():
                recette_choisie_id = str(row["Recette"])
                if self.recette_manager.obtenir_nom(recette_choisie_id) is None:
                    recette_choisie_id = None
                    self._ajouter_resultat(resultats_menu, date_repas, nom_repas, participants_str, "ERREUR: Recette pré-remplie inexistante.", 0)
                else:
                    self._log_decision_recette(recette_choisie_id, date_repas, participants_str)
            else:
                if "Repas" in row and str(row["Repas"]).lower() == "resto":
                    self._ajouter_resultat(resultats_menu, date_repas, "Restaurant", participants_str, "Menu restaurant", 0)
                    continue
                elif "Repas" in row and str(row["Repas"]).lower() == "pizzas":
                    self._ajouter_resultat(resultats_menu, date_repas, "Pizzas", participants_str, "Soirée pizzas", 0)
                    continue

                recette_choisie_id, manquants_recette = self._traiter_menu_standard(
                    date_repas, participants_str, participants_count_int,
                    used_recipes_in_current_gen, menu_recent_noms,
                    transportable_req, temps_req, nutrition_req
                )
            
            if recette_choisie_id:
                nom_recette = self.recette_manager.obtenir_nom(recette_choisie_id)
                used_recipes_in_current_gen.add(recette_choisie_id)
                menu_recent_noms.insert(0, nom_recette)
                menu_recent_noms = menu_recent_noms[:5]

                qte_necessaire = self.recette_manager.calculer_quantite_necessaire(recette_choisie_id, participants_count_int)
                for ing_id, qte in qte_necessaire.items():
                    ingredients_menu_cumules[ing_id] = ingredients_menu_cumules.get(ing_id, 0) + qte

                self.recette_manager.decrementer_stock(recette_choisie_id, participants_count_int, date_repas)
                
                temps_total_int = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
                remarques_recette = (
                    "Recette pré-remplie" if "Recette" in row and pd.notna(row["Recette"]) else
                    "Généré automatiquement"
                )
                self._ajouter_resultat(resultats_menu, date_repas, nom_repas, participants_str, remarques_recette, temps_total_int, recette_choisie_id)
            else:
                remarques_finales = "Pas de recette trouvée (génération impossible avec les critères)."
                self._ajouter_resultat(resultats_menu, date_repas, nom_repas, participants_str, remarques_finales, 0)
                
        return pd.DataFrame(resultats_menu), self.calculer_liste_courses(ingredients_menu_cumules)

    def calculer_liste_courses(self, ingredients_menu_cumules):
        liste_courses = []
        for ing_id, qte_necessaire in ingredients_menu_cumules.items():
            nom_ingredient = self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)
            unite = self.recette_manager.obtenir_unite_ingredient_par_id(ing_id)
            
            qte_stock_initial = self.recette_manager.obtenir_qte_stock_par_id(ing_id)
            
            qte_a_acheter = max(0, qte_necessaire - qte_stock_initial)
            
            if qte_a_acheter > 0:
                liste_courses.append({
                    "Ingrédient": nom_ingredient,
                    "Quantité du menu": f"{qte_necessaire:.2f} {unite}",
                    "Quantité stock": f"{qte_stock_initial:.2f} {unite}",
                    "Quantité à acheter": f"{qte_a_acheter:.2f} {unite}"
                })
        return liste_courses


def main():
    st.set_page_config(page_title="Générateur de Menus Automatique", layout="wide")
    st.title("Générateur de Menus Automatique")

    df_planning_data = {
        "Date": [datetime.now() + timedelta(days=i) for i in range(8)], # <-- Modifié ici
        "Repas": ["Midi", "Soir"] * 4,
        "Participant(s)": ["P", "A,B"] * 4,
        "Recette": [None] * 8,
        "Transportable": ["Non"] * 8,
        "Temps de préparation": [""] * 8,
        "Nutrition": [""] * 8
    }
    df_planning_default = pd.DataFrame(df_planning_data)
    df_planning_default["Repas"] = df_planning_default["Repas"].replace(
        {"Midi": "Midi", "Soir": "Soir"}
    )
    df_planning_default["Participant(s)"] = df_planning_default["Participant(s)"].replace(
        {"A,B": "A,B"}
    )

    if 'planning' not in st.session_state:
        st.session_state.planning = df_planning_default

    st.header("1. Planning de la semaine")
    st.info("Remplissez le planning. Laissez 'Recette' vide pour une génération automatique.")

    edited_df = st.data_editor(
        st.session_state.planning,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "Date": st.column_config.DatetimeColumn(
                "Date",
                format="DD/MM/YYYY",
                step=1
            ),
            "Repas": st.column_config.SelectboxColumn(
                "Repas",
                options=["Midi", "Soir", "Pizzas", "Restaurant"],
                required=True
            ),
            "Participant(s)": st.column_config.TextColumn(
                "Participant(s)",
                help="Séparer les codes par des virgules (ex: A,B,C)",
                required=True
            ),
            "Recette": st.column_config.TextColumn("Recette (ID Notion)"),
            "Transportable": st.column_config.SelectboxColumn(
                "Transportable",
                options=["Oui", "Non", ""],
            ),
            "Temps de préparation": st.column_config.SelectboxColumn(
                "Temps de préparation",
                options=["express", "rapide", ""],
            ),
            "Nutrition": st.column_config.SelectboxColumn(
                "Nutrition",
                options=["équilibré", ""],
            )
        }
    )
    st.session_state.planning = edited_df
    
    if st.button("Générer le menu et la liste de courses"):
        with st.spinner("Génération en cours..."):
            try:
                df_recettes = extract_recettes()
                verifier_colonnes(df_recettes, HDR_RECETTES, "Recettes")

                df_menus_hist = extract_menus()
                verifier_colonnes(df_menus_hist, HDR_MENUS, "Historique des menus")
                
                df_ingredients = extract_ingredients()
                verifier_colonnes(df_ingredients, HDR_INGR, "Ingrédients")
                
                df_ingr_rec = extract_ingr_rec()
                verifier_colonnes(df_ingr_rec, HDR_IR, "Ingrédients/Recettes")

                menu_generator = MenuGenerator(
                    df_menus_hist, df_recettes, edited_df, df_ingredients, df_ingr_rec
                )
                menu_genere, liste_courses = menu_generator.generer_menu()
                
                st.header("2. Menu de la semaine généré")
                st.dataframe(menu_genere)

                df_export = menu_genere.drop(columns=['Recette_ID'])
                df_export = df_export.rename(columns={'Repas': 'Type Repas', 'Remarques': 'Remarques sur la génération'})
                df_export['Nom'] = [
                    menu_generator.recette_manager.obtenir_nom(rec_id) 
                    if rec_id else "" 
                    for rec_id in menu_genere['Recette_ID']
                ]
                df_export = df_export[['Date', 'Type Repas', 'Participant(s)', 'Nom']]
                
                csv_data = df_export.to_csv(index=False, sep=',', encoding='utf-8-sig')
                
                st.download_button(
                    label="📥 Télécharger le menu en CSV",
                    data=csv_data,
                    file_name="menu_genere.csv",
                    mime="text/csv"
                )

                st.header("3. Liste de Courses Détaillée")
                if liste_courses:
                    liste_courses_df = pd.DataFrame(liste_courses)
                    st.dataframe(liste_courses_df)

                    csv = liste_courses_df.to_csv(index=False, sep=';', encoding='utf-8-sig')
                    st.download_button(
                        label="Télécharger la liste de courses (CSV)",
                        data=csv,
                        file_name="liste_courses.csv",
                        mime="text/csv",
                    )
                else:
                    st.info("Aucun ingrédient manquant identifié pour la liste de courses.")

            except ValueError as ve:
                st.error(f"Erreur de données: {ve}")
                logger.exception("Erreur de données lors de la génération du menu")
            except Exception as e:
                st.error(f"Une erreur inattendue est survenue lors de la génération: {e}")
                logger.exception("Erreur inattendue lors de la génération du menu dans Streamlit")

if __name__ == "__main__":
    main()
