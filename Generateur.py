import streamlit as st
import pandas as pd
import logging
import re
from datetime import datetime, timedelta
from notion_client import Client
from notion_client.helpers import get_id
import io

# --- Configuration du logger ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Constantes globales (à adapter si vos noms de colonnes réels sont différents) ---
COLONNE_ID_RECETTE = "Recette ID"
COLONNE_NOM = "Nom"
COLONNE_TEMPS_TOTAL = "Temps total (min)"
COLONNE_AIME_PAS_PRINCIP = "Aime pas (principal)"
COLONNE_ID_INGREDIENT = "ID Ingrédient" # Assurez-vous que c'est le même nom dans votre fichier Ingredients.csv
FICHIER_SORTIE_MENU_CSV = "Menus_generes.csv"
FICHIER_SORTIE_LISTES_TXT = "Listes_ingredients.txt"


# --- Fonctions utilitaires ---
def verifier_colonnes(df, colonnes_attendues, nom_fichier):
    """Vérifie la présence des colonnes attendues dans un DataFrame."""
    missing_cols = [col for col in colonnes_attendues if col not in df.columns]
    if missing_cols:
        logger.warning(f"Colonnes manquantes dans {nom_fichier}: {', '.join(missing_cols)}")
        st.warning(f"Certaines colonnes attendues sont manquantes dans '{nom_fichier}'. "
                   f"Veuillez vérifier : {', '.join(missing_cols)}")
        # Vous pourriez choisir de lever une erreur ici ou de continuer avec un avertissement.
        # Pour une application Streamlit, un avertissement et la poursuite est souvent préférable.
    else:
        logger.info(f"Toutes les colonnes attendues trouvées dans {nom_fichier}.")

# --- Classes de logique métier ---

class RecetteManager:
    """Gère les recettes, les ingrédients et le stock."""
    def __init__(self, df_recettes, df_ingredients, df_ingredients_recettes):
        self.df_recettes = df_recettes.copy()
        self.df_ingredients_initial = df_ingredients.copy()
        self.df_ingredients_recettes = df_ingredients_recettes.copy()
        self.stock_simule = df_ingredients.copy()

        # Nettoyage des noms de colonnes dans les DataFrames
        self.df_recettes.columns = [col.strip() for col in self.df_recettes.columns]
        self.df_ingredients_initial.columns = [col.strip() for col in self.df_ingredients_initial.columns]
        self.df_ingredients_recettes.columns = [col.strip() for col in self.df_ingredients_recettes.columns]
        self.stock_simule.columns = [col.strip() for col in self.stock_simule.columns]

        # Vérification et préparation des colonnes essentielles
        if COLONNE_ID_INGREDIENT not in self.stock_simule.columns:
            raise ValueError(f"Colonne '{COLONNE_ID_INGREDIENT}' manquante dans le fichier Ingredients.csv.")
        if 'Qte reste' not in self.stock_simule.columns:
            raise ValueError(f"Colonne 'Qte reste' manquante dans le fichier Ingredients.csv.")
        if 'unité' not in self.df_ingredients_initial.columns:
            raise ValueError(f"Colonne 'unité' manquante dans le fichier Ingredients.csv.")
        if COLONNE_ID_RECETTE not in self.df_recettes.columns:
            raise ValueError(f"Colonne '{COLONNE_ID_RECETTE}' manquante dans le fichier Recettes.csv.")
        if COLONNE_NOM not in self.df_recettes.columns:
            raise ValueError(f"Colonne '{COLONNE_NOM}' manquante dans le fichier Recettes.csv.")
        if COLONNE_ID_RECETTE not in self.df_ingredients_recettes.columns:
            raise ValueError(f"Colonne '{COLONNE_ID_RECETTE}' manquante dans le fichier Ingredients_recettes.csv.")
        if "Qté/pers_s" not in self.df_ingredients_recettes.columns:
             raise ValueError(f"Colonne 'Qté/pers_s' manquante dans le fichier Ingredients_recettes.csv.")
        if "Ingrédient ok" not in self.df_ingredients_recettes.columns:
             raise ValueError(f"Colonne 'Ingrédient ok' manquante dans le fichier Ingredients_recettes.csv.")


        self.stock_simule['Qte reste'] = pd.to_numeric(self.stock_simule['Qte reste'], errors='coerce').fillna(0)
        self.df_ingredients_initial['unité'] = self.df_ingredients_initial['unité'].astype(str)

        # Assurez-vous que la colonne 'Recette ID' est du même type pour la jointure
        self.df_recettes[COLONNE_ID_RECETTE] = self.df_recettes[COLONNE_ID_RECETTE].astype(str)
        self.df_ingredients_recettes[COLONNE_ID_RECETTE] = self.df_ingredients_recettes[COLONNE_ID_RECETTE].astype(str)
        self.df_ingredients_initial[COLONNE_ID_INGREDIENT] = self.df_ingredients_initial[COLONNE_ID_INGREDIENT].astype(str)
        self.stock_simule[COLONNE_ID_INGREDIENT] = self.stock_simule[COLONNE_ID_INGREDIENT].astype(str)


        self.stock_simule.set_index(COLONNE_ID_INGREDIENT, inplace=True)
        self._anti_gaspi_ingredients = self._trouver_ingredients_stock_eleve()
        logger.info(f"Ingrédients anti-gaspi identifiés: {self._anti_gaspi_ingredients}")


    def get_ingredients_for_recipe(self, recette_id):
        """Récupère la liste des ingrédients et leurs quantités pour une recette donnée."""
        recette_id = str(recette_id)
        ingredients_data = self.df_ingredients_recettes[self.df_ingredients_recettes[COLONNE_ID_RECETTE] == recette_id]
        if ingredients_data.empty:
            logger.warning(f"Aucun ingrédient trouvé pour la recette ID: {recette_id}")
            return pd.DataFrame() # Retourne un DataFrame vide si aucun ingrédient

        # Assurez-vous que 'Ingrédient ok' est du même type que COLONNE_ID_INGREDIENT dans df_ingredients_initial
        ingredients_data = pd.merge(ingredients_data, self.df_ingredients_initial[[COLONNE_ID_INGREDIENT, 'Nom', 'unité']],
                                    left_on='Ingrédient ok', right_on=COLONNE_ID_INGREDIENT, how='left')
        return ingredients_data

    def _trouver_ingredients_stock_eleve(self):
        """Identifie les ingrédients dont le stock est élevé pour l'anti-gaspillage."""
        # Un seuil "élevé" est arbitraire ici, peut être configuré
        seuil_eleve = 5 # Exemple: plus de 5 unités
        ingredients_eleves = self.stock_simule[self.stock_simule['Qte reste'] >= seuil_eleve]
        return set(ingredients_eleves.index.astype(str))

    def recette_utilise_ingredient_anti_gaspi(self, recette_id):
        """Vérifie si une recette utilise un ingrédient en stock élevé."""
        ingredients_recette = self.get_ingredients_for_recipe(recette_id)
        if ingredients_recette.empty:
            return False
        # Assurez-vous que 'Ingrédient ok' est bien l'ID de l'ingrédient dans ingredients_recette
        ingredients_ids = set(ingredients_recette['Ingrédient ok'].astype(str))
        return not self._anti_gaspi_ingredients.isdisjoint(ingredients_ids)


    def calculer_quantite_necessaire(self, ing_id, qte_par_pers, nb_pers):
        """Calcule la quantité totale nécessaire d'un ingrédient."""
        # Gérer les cas où qte_par_pers n'est pas un nombre
        try:
            qte_par_pers_float = float(str(qte_par_pers).replace(',', '.'))
        except ValueError:
            logger.warning(f"Quantité par personne non numérique pour ingrédient {ing_id}: {qte_par_pers}")
            return 0.0
        return qte_par_pers_float * nb_pers

    def evaluer_disponibilite_et_manquants(self, recette_id, nb_personnes):
        """Évalue la disponibilité des ingrédients pour une recette et retourne les manquants."""
        ingredients_recette = self.get_ingredients_for_recipe(recette_id)
        if ingredients_recette.empty:
            return 0, 0, {} # Aucun ingrédient, pas de disponibilité

        total_ingredients_necessaires = 0
        ingredients_disponibles_count = 0
        ingredients_manquants = {}

        for _, ing_row in ingredients_recette.iterrows():
            ing_id = str(ing_row['Ingrédient ok'])
            qte_par_pers = ing_row['Qté/pers_s']
            qte_necessaire = self.calculer_quantite_necessaire(ing_id, qte_par_pers, nb_personnes)

            if qte_necessaire <= 0:
                continue # Ignore les ingrédients avec quantité 0 ou négative

            total_ingredients_necessaires += 1
            stock_actuel = self.stock_simule.loc[ing_id, 'Qte reste'] if ing_id in self.stock_simule.index else 0

            if stock_actuel >= qte_necessaire:
                ingredients_disponibles_count += 1
            else:
                manquant = qte_necessaire - stock_actuel
                ingredients_manquants[ing_id] = manquant

        if total_ingredients_necessaires == 0:
            return 0, 0, {} # Éviter la division par zéro

        score_disponibilite = ingredients_disponibles_count
        pourcentage_disponibilite = (ingredients_disponibles_count / total_ingredients_necessaires) * 100

        return score_disponibilite, pourcentage_disponibilite, ingredients_manquants


    def decrementer_stock(self, recette_id, nb_personnes, date_repas_dt):
        """Décrémente le stock simulé des ingrédients utilisés par une recette."""
        ingredients_consommes_ids = []
        ingredients_recette = self.get_ingredients_for_recipe(recette_id)

        if ingredients_recette.empty:
            logger.warning(f"Tentative de décrémenter le stock pour recette ID {recette_id} sans ingrédients.")
            return []

        for _, ing_row in ingredients_recette.iterrows():
            ing_id = str(ing_row['Ingrédient ok'])
            qte_par_pers = ing_row['Qté/pers_s']
            qte_necessaire = self.calculer_quantite_necessaire(ing_id, qte_par_pers, nb_personnes)

            if qte_necessaire > 0:
                if ing_id in self.stock_simule.index:
                    self.stock_simule.loc[ing_id, 'Qte reste'] = max(0, self.stock_simule.loc[ing_id, 'Qte reste'] - qte_necessaire)
                    ingredients_consommes_ids.append(ing_id)
                else:
                    logger.warning(f"Ingrédient ID {ing_id} de la recette {recette_id} non trouvé dans le stock. Non décrémenté.")
        return ingredients_consommes_ids

    def obtenir_nom(self, recette_id):
        """Retourne le nom d'une recette par son ID."""
        recette_id = str(recette_id)
        nom = self.df_recettes.loc[self.df_recettes[COLONNE_ID_RECETTE] == recette_id, COLONNE_NOM]
        return nom.iloc[0] if not nom.empty else f"Recette_ID_{recette_id}_Non_Trouvee"

    def obtenir_nom_ingredient_par_id(self, ing_id):
        """Retourne le nom d'un ingrédient par son ID."""
        ing_id = str(ing_id)
        nom = self.df_ingredients_initial.loc[self.df_ingredients_initial[COLONNE_ID_INGREDIENT] == ing_id, 'Nom']
        return nom.iloc[0] if not nom.empty else f"ID_Ing_{ing_id}_Non_Trouve"

    def est_adaptee_aux_participants(self, recette_id, participants_str):
        """Vérifie si une recette est adaptée aux participants (gestion des 'aime pas')."""
        recette_data = self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE] == str(recette_id)]
        if recette_data.empty:
            return True # Recette non trouvée, considérer comme adaptée par défaut ou lever une erreur

        aime_pas_str = recette_data[COLONNE_AIME_PAS_PRINCIP].iloc[0]
        if pd.isna(aime_pas_str) or aime_pas_str == "":
            return True

        aime_pas_list = [item.strip() for item in str(aime_pas_str).split(',')]
        participants_list = [p.strip() for p in participants_str.split('+')]

        for participant in participants_list:
            if participant in aime_pas_list:
                return False
        return True

    def est_transportable(self, recette_id):
        """Vérifie si une recette est marquée comme transportable."""
        recette_data = self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE] == str(recette_id)]
        if recette_data.empty or 'Transportable' not in recette_data.columns:
            return False
        # Assurez-vous que la colonne 'Transportable' est de type booléen ou gérée comme telle
        transportable_val = recette_data['Transportable'].iloc[0]
        # Gérer les cas où c'est une chaîne "Oui", "Non", ou booléen
        if isinstance(transportable_val, str):
            return transportable_val.lower() == 'oui'
        return bool(transportable_val)


    def obtenir_temps_preparation(self, recette_id):
        """Retourne le temps de préparation total d'une recette."""
        recette_data = self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE] == str(recette_id)]
        if recette_data.empty or COLONNE_TEMPS_TOTAL not in recette_data.columns:
            return None
        temps = pd.to_numeric(recette_data[COLONNE_TEMPS_TOTAL].iloc[0], errors='coerce')
        return temps if not pd.isna(temps) else None

class MenusHistoryManager:
    """Gère l'historique des menus déjà servis."""
    def __init__(self, df_menus_hist):
        self.df_menus_hist = df_menus_hist.copy()
        if not self.df_menus_hist.empty:
            # Nettoyage des noms de colonnes
            self.df_menus_hist.columns = [col.strip() for col in self.df_menus_hist.columns]
            if 'Date' in self.df_menus_hist.columns and pd.api.types.is_string_dtype(self.df_menus_hist['Date']):
                self.df_menus_hist['Date'] = pd.to_datetime(self.df_menus_hist['Date'], errors='coerce', dayfirst=True) # Utiliser dayfirst=True
            self.df_menus_hist.dropna(subset=['Date'], inplace=True)
            if 'Semaine' not in self.df_menus_hist.columns:
                self.df_menus_hist['Semaine'] = self.df_menus_hist['Date'].dt.isocalendar().week.astype(int)
            if 'Recette' in self.df_menus_hist.columns:
                self.df_menus_hist['Recette'] = self.df_menus_hist['Recette'].astype(str) # Assurer le type str pour l'ID recette
        else:
            logger.info("df_menus_hist est vide à l'initialisation de MenusHistoryManager.")

class MenuGenerator:
    """Génère un menu complet en fonction des contraintes."""
    def __init__(self, menus_history_manager, recette_manager, df_planning):
        self.menus_history_manager = menus_history_manager
        self.recette_manager = recette_manager
        self.df_planning = df_planning.copy() # Travailler sur une copie
        self.ingredients_a_acheter_cumules = {} # Nouvelle initialisation ici

        # Nettoyage des noms de colonnes du planning
        self.df_planning.columns = [col.strip() for col in self.df_planning.columns]

        logger.info("MenuGenerator initialisé.")

    def recettes_meme_semaine_annees_precedentes(self, date_repas_dt):
        """Trouve les recettes utilisées la même semaine les années précédentes."""
        if self.menus_history_manager.df_menus_hist.empty:
            return set()

        semaine_actuelle = date_repas_dt.isocalendar().week
        historique_semaine = self.menus_history_manager.df_menus_hist[
            self.menus_history_manager.df_menus_hist['Semaine'] == semaine_actuelle
        ]
        return set(historique_semaine['Recette'].astype(str).unique())

    def est_recente(self, recette_nom, date_repas_dt, menu_recent_noms):
        """Vérifie si une recette a été utilisée récemment pour éviter la répétition."""
        # Vérifier dans le menu généré pour la génération actuelle (derniers 3 repas)
        if recette_nom in menu_recent_noms:
            return True

        # Vérifier dans l'historique global (par exemple, sur les 30 derniers jours)
        if not self.menus_history_manager.df_menus_hist.empty:
            date_limite = date_repas_dt - timedelta(days=30)
            recent_history = self.menus_history_manager.df_menus_hist[
                (self.menus_history_manager.df_menus_hist['Date'] >= date_limite) &
                (self.menus_history_manager.df_menus_hist['Nom Menu'] == recette_nom) # Assurez-vous que 'Nom Menu' contient le nom de la recette
            ]
            if not recent_history.empty:
                return True
        return False

    def compter_participants(self, participants_str):
        """Compte le nombre de participants à partir d'une chaîne de caractères."""
        if pd.isna(participants_str) or str(participants_str).strip() == "":
            return 0
        return len(str(participants_str).split('+'))


    def _filtrer_recette_base(self, df_candidates, date_repas_dt, participants_str, menu_recent_noms, transportable_req, temps_req, nutrition_req):
        """Applique les filtres de base aux recettes candidates."""
        # Filtrer les recettes récentes
        df_candidates_filtered = df_candidates[
            ~df_candidates[COLONNE_NOM].apply(lambda x: self.est_recente(x, date_repas_dt, menu_recent_noms))
        ].copy()

        # Filtrer par "Aime pas"
        df_candidates_filtered = df_candidates_filtered[
            df_candidates_filtered[COLONNE_ID_RECETTE].apply(lambda x: self.recette_manager.est_adaptee_aux_participants(x, participants_str))
        ].copy()

        # Filtrer par transportable si requis
        if transportable_req:
            df_candidates_filtered = df_candidates_filtered[
                df_candidates_filtered[COLONNE_ID_RECETTE].apply(self.recette_manager.est_transportable)
            ].copy()

        # Filtrer par temps de préparation max
        if temps_req is not None:
            df_candidates_filtered = df_candidates_filtered[
                df_candidates_filtered[COLONNE_ID_RECETTE].apply(
                    lambda x: self.recette_manager.obtenir_temps_preparation(x) is None or self.recette_manager.obtenir_temps_preparation(x) <= temps_req
                )
            ].copy()

        # Filtrer par nutrition (logique simplifiée, à développer si besoin de plus de critères)
        if nutrition_req:
            # Exemple: Supposons que 'Nutrition' soit une colonne avec des tags comme "Végétarien", "Léger"
            # Adapter selon la structure réelle de votre df_recettes
            if 'Nutrition' in df_candidates_filtered.columns:
                 df_candidates_filtered = df_candidates_filtered[df_candidates_filtered['Nutrition'].astype(str).str.contains(nutrition_req, case=False, na=False)]


        return df_candidates_filtered

    def generer_recettes_candidates(self, date_repas_dt, participants_str, participants_count, menu_recent_noms, transportable_req, temps_req, nutrition_req, type_repas):
        """Génère une liste de recettes candidates avec des scores."""
        # Commencez avec toutes les recettes disponibles
        df_candidates = self.recette_manager.df_recettes.copy()

        # Appliquer les filtres de base (récence, aime pas, transportable, temps, nutrition)
        df_candidates_filtered = self._filtrer_recette_base(
            df_candidates, date_repas_dt, participants_str, menu_recent_noms, transportable_req, temps_req, nutrition_req
        )

        if df_candidates_filtered.empty:
            logger.info(f"Aucune recette restante après les filtres de base pour {date_repas_dt} ({type_repas}).")
            return []

        # Calculer les scores de disponibilité et identifier les manquants
        candidates_with_scores = []
        for _, recette_row in df_candidates_filtered.iterrows():
            recette_id = str(recette_row[COLONNE_ID_RECETTE])
            score_disponibilite, pourcentage_disponibilite, ingredients_manquants = \
                self.recette_manager.evaluer_disponibilite_et_manquants(recette_id, participants_count)

            # Priorité anti-gaspillage
            anti_gaspi_score = 10 if self.recette_manager.recette_utilise_ingredient_anti_gaspi(recette_id) else 0

            # Priorité historique (recettes de la même semaine d'années précédentes)
            historical_score = 0
            if recette_id in self.recettes_meme_semaine_annees_precedentes(date_repas_dt):
                historical_score = 5

            # Score global: disponibilité est clé, puis anti-gaspi, puis historique
            # On privilégie les recettes avec le moins d'ingrédients manquants
            # Le score de disponibilité est inversé pour que moins de manquants = score plus élevé
            total_score = (score_disponibilite * 100) + anti_gaspi_score + historical_score

            candidates_with_scores.append({
                'recette_id': recette_id,
                'nom_recette': recette_row[COLONNE_NOM],
                'score_disponibilite': score_disponibilite,
                'pourcentage_disponibilite': pourcentage_disponibilite,
                'ingredients_manquants': ingredients_manquants,
                'total_score': total_score
            })
        
        # Triez les candidats : d'abord par score global (décroissant), puis par % de disponibilité (décroissant)
        # puis par nombre d'ingrédients manquants (croissant).
        # Un score plus élevé signifie une meilleure adéquation.
        candidates_with_scores_sorted = sorted(
            candidates_with_scores,
            key=lambda x: (
                x['total_score'],
                x['pourcentage_disponibilite'],
                -len(x['ingredients_manquants']) # Moins de manquants = mieux
            ),
            reverse=True
        )

        logger.info(f"{len(candidates_with_scores_sorted)} recettes candidates générées pour {date_repas_dt} ({type_repas}).")
        return candidates_with_scores_sorted

    def _traiter_menu_standard(self, date_repas_dt, participants_str, participants_count, used_recipes_current_generation_set, menu_recent_noms, transportable_req, temps_req, nutrition_req):
        """Logic for standard meal selection, returns chosen recipe ID and missing ingredients."""
        candidates = self.generer_recettes_candidates(
            date_repas_dt, participants_str, participants_count,
            menu_recent_noms, transportable_req, temps_req, nutrition_req, "Standard"
        )

        for candidate in candidates:
            recette_id = candidate['recette_id']
            # Ne pas proposer une recette déjà utilisée dans cette génération
            if recette_id in used_recipes_current_generation_set:
                continue

            # Ne pas proposer une recette qui a trop d'ingrédients manquants (seuil configurable)
            if len(candidate['ingredients_manquants']) > 3: # Exemple: pas plus de 3 ingrédients à acheter
                continue

            logger.info(f"Recette standard choisie pour {date_repas_dt}: {candidate['nom_recette']}")
            return recette_id, candidate['ingredients_manquants']

        logger.info(f"Aucune recette standard adéquate trouvée pour {date_repas_dt}.")
        return None, {}

    def _log_decision_recette(self, recette_id, date_repas_dt, participants_str):
        """Log the chosen recipe."""
        nom_recette = self.recette_manager.obtenir_nom(recette_id)
        logger.info(f"Décision: Recette '{nom_recette}' (ID: {recette_id}) choisie pour le {date_repas_dt.strftime('%d/%m/%Y')} avec {participants_str}.")

    def generer_menu_repas_b(self, repas_type, date_repas_dt, participants_str, participants_count, plats_transportables_semaine, menu_recent_noms, used_recipes_current_generation_set):
        """Génère un menu de type 'Reste'."""
        candidats_restes = []
        for date_plat_transportable, recette_id in plats_transportables_semaine.items():
            # Ne pas proposer de restes de la même journée
            if date_plat_transportable.date() == date_repas_dt.date():
                continue

            # Vérifier si la recette a déjà été utilisée dans la génération actuelle
            if recette_id in used_recipes_current_generation_set:
                continue

            # Vérifier la fraîcheur du plat (par exemple, reste de moins de 3 jours)
            if (date_repas_dt - date_plat_transportable).days > 3:
                continue

            # Vérifier si c'est adapté aux participants (si applicable pour les restes)
            if not self.recette_manager.est_adaptee_aux_participants(recette_id, participants_str):
                continue

            # Vérifier si la recette est "récente" (déjà mangée souvent)
            if self.est_recente(self.recette_manager.obtenir_nom(recette_id), date_repas_dt, menu_recent_noms):
                continue

            # Évaluer la quantité de reste disponible (basé sur le stock simulé ou une logique spécifique)
            # Pour l'exemple, supposons qu'un plat transportable signifie qu'il y a assez pour le repas.
            # Dans un cas réel, vous auriez besoin d'une gestion plus fine des "restes disponibles".
            score_dispo, _, _ = self.recette_manager.evaluer_disponibilite_et_manquants(recette_id, participants_count)
            if score_dispo <= 0: # Si les ingrédients sont épuisés pour cette recette dans le stock simulé
                 continue

            candidats_restes.append({
                'recette_id': recette_id,
                'date_origine': date_plat_transportable,
                'score': score_dispo # Utiliser le score de dispo comme critère principal
            })

        if candidats_restes:
            # Trier par le reste le plus ancien pour le consommer en premier (FIFO), ou par score de dispo
            candidats_restes_tries = sorted(candidats_restes, key=lambda x: (x['date_origine'], x['score']), reverse=False)
            choix_reste = candidats_restes_tries[0]
            del plats_transportables_semaine[choix_reste['date_origine']] # Supprimer le reste utilisé
            logger.info(f"Reste choisi pour le {date_repas_dt}: {self.recette_manager.obtenir_nom(choix_reste['recette_id'])}")
            return choix_reste['recette_id'], "Reste utilisé"
        else:
            logger.info(f"Aucun reste disponible ou adéquat pour le {repas_type} du {date_repas_dt}.")
            return None, "Pas de reste disponible"


    def _ajouter_resultat(self, resultats_df_list, date_repas_dt, nom_plat_final, participants_str, remarques_repas, temps_prep_final, recette_choisie_id):
        """Ajoute un résultat de repas à la liste des résultats."""
        resultats_df_list.append({
            "Date": date_repas_dt,
            "Type_Repas": date_repas_dt.strftime('%H:%M'), # Ou extraire du planning si disponible
            "Nom Plat Final": nom_plat_final,
            "Participant(s)": participants_str,
            "Remarques": remarques_repas,
            "Temps Préparation": temps_prep_final,
            "Recette ID": recette_choisie_id
        })

    def generer_menu(self):
        """Génère le menu complet pour la période planifiée."""
        self.ingredients_a_acheter_cumules = {} # Réinitialise la liste de courses cumulées
        resultats_df_list = []
        menu_recent_noms = [] # Garder trace des 3 derniers noms de repas pour éviter les répétitions immédiates
        used_recipes_current_generation_set = set() # Pour éviter les duplicatas dans la même génération
        plats_transportables_semaine = {} # Pour suivre les plats transportables de la semaine

        ingredients_effectivement_utilises_ids_set = set()

        logger.info("Début de la génération du menu.")

        # Préparez le DataFrame de planification : assurez-vous que 'Date' est en datetime et trié
        if 'Date' in self.df_planning.columns:
            self.df_planning['Date'] = pd.to_datetime(self.df_planning['Date'], errors="coerce", dayfirst=True)
            self.df_planning.dropna(subset=["Date"], inplace=True)
            self.df_planning = self.df_planning.sort_values(by="Date").reset_index(drop=True)
        else:
            logger.error("La colonne 'Date' est manquante dans le DataFrame de planification.")
            return pd.DataFrame(), [], [], []

        # Assurez-vous que les colonnes nécessaires sont présentes dans df_planning ou fournissez des valeurs par défaut
        required_planning_cols = ["Date", "Participants"]
        for col in required_planning_cols:
            if col not in self.df_planning.columns:
                logger.error(f"Colonne '{col}' manquante dans le Planning.csv. Impossible de générer le menu.")
                st.error(f"La colonne '{col}' est manquante dans votre fichier Planning.csv. Veuillez la corriger.")
                return pd.DataFrame(), [], [], []

        # Pour le type de repas, si votre planning est structuré par colonnes comme "Déjeuner", "Dîner", etc.
        # vous devez le "dépivoter" ou adapter la boucle.
        # Si votre planning a déjà une colonne 'Type_Repas', c'est plus simple.
        # Le code Streamlit précédent suggérait un dépivotement, adaptons la boucle pour cela.
        planning_melted = self.df_planning.melt(id_vars=['Date', 'Participants'], var_name='Type_Repas_Raw', value_name='Recette_Nom_Prevue')
        # Filtrer les lignes où la recette prévue est vide/NaN
        planning_melted = planning_melted[planning_melted['Recette_Nom_Prevue'].notna() & (planning_melted['Recette_Nom_Prevue'] != '')]

        # Nettoyer 'Type_Repas_Raw' pour obtenir 'Type_Repas'
        planning_melted['Type_Repas'] = planning_melted['Type_Repas_Raw'].str.replace('_', ' ').str.title()
        planning_melted['Type_Repas'] = planning_melted['Type_Repas'].replace({
            'Dejeuner': 'Déjeuner',
            'Diner': 'Dîner'
        })
        
        # Ajouter des colonnes par défaut si elles ne sont pas dans le planning d'origine
        # Ces valeurs par défaut peuvent être modifiées ou rendues configurables par l'utilisateur dans Streamlit
        planning_melted['Transportable'] = False # Default value
        planning_melted['Temps_Max'] = None      # Default value
        planning_melted['Nutrition'] = None      # Default value

        # Trier le planning dépivoté par date
        planning_melted = planning_melted.sort_values(by="Date").reset_index(drop=True)

        for index, row in planning_melted.iterrows():
            date_repas_dt = row["Date"]
            participants_str = str(row["Participants"])
            type_repas = str(row["Type_Repas"])
            transportable_req = row.get("Transportable", False)
            temps_req = row.get("Temps_Max", None)
            nutrition_req = row.get("Nutrition", None)

            participants_count = self.compter_participants(participants_str)

            nom_plat_final = "Non défini"
            remarques_repas = ""
            temps_prep_final = None
            recette_choisie_id = None
            ingredients_manquants_pour_recette_choisie = {}
            ingredients_consommes_ce_repas = None

            if type_repas == "Déjeuner" and date_repas_dt.weekday() >= 5 and plats_transportables_semaine:
                recette_choisie_id, remarques_repas = self.generer_menu_repas_b(
                    type_repas, date_repas_dt, participants_str, participants_count,
                    plats_transportables_semaine, menu_recent_noms, used_recipes_current_generation_set
                )
                if recette_choisie_id:
                    nom_plat_final = self.recette_manager.obtenir_nom(recette_choisie_id)
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
                    used_recipes_current_generation_set.add(recette_choisie_id)
                    ingredients_consommes_ce_repas = self.recette_manager.decrementer_stock(recette_choisie_id, participants_count, date_repas_dt)
                else:
                    remarques_repas = "Pas de reste transportable disponible, recherche de recette standard."
                    recette_choisie_id, ingredients_manquants_pour_recette_choisie = self._traiter_menu_standard(
                        date_repas_dt, participants_str, participants_count,
                        used_recipes_current_generation_set, menu_recent_noms,
                        transportable_req, temps_req, nutrition_req
                    )
                    if recette_choisie_id:
                        nom_plat_final = self.recette_manager.obtenir_nom(recette_choisie_id)
                        if nom_plat_final and "Recette_ID_" not in nom_plat_final:
                            temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
                            used_recipes_current_generation_set.add(recette_choisie_id)
                            for ing_id_manquant, qte_manquante in ingredients_manquants_pour_recette_choisie.items():
                                self.ingredients_a_acheter_cumules[ing_id_manquant] = self.ingredients_a_acheter_cumules.get(ing_id_manquant, 0) + qte_manquante
                            ingredients_consommes_ce_repas = self.recette_manager.decrementer_stock(recette_choisie_id, participants_count, date_repas_dt)
                            self._log_decision_recette(recette_choisie_id, date_repas_dt, participants_str)
                        else:
                            nom_plat_final = f"Recette ID {recette_choisie_id} - Nom Invalide"
                            remarques_repas = "Erreur: Nom de recette non trouvé pour cet ID."
                            recette_choisie_id = None
                    else:
                        nom_plat_final = "Pas de recette trouvée"
                        remarques_repas = "Aucune recette candidate ne correspond aux critères."
            elif type_repas == "Reste":
                recette_choisie_id, remarques_repas = self.generer_menu_repas_b(
                    type_repas, date_repas_dt, participants_str, participants_count,
                    plats_transportables_semaine, menu_recent_noms, used_recipes_current_generation_set
                )
                if recette_choisie_id:
                    nom_plat_final = self.recette_manager.obtenir_nom(recette_choisie_id)
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
                    used_recipes_current_generation_set.add(recette_choisie_id)
                    ingredients_consommes_ce_repas = self.recette_manager.decrementer_stock(recette_choisie_id, participants_count, date_repas_dt)
                else:
                    nom_plat_final = "Pas de reste disponible"
                    remarques_repas = "Aucun reste n'a pu être trouvé ou ne correspondait aux critères."
            else: # Repas standard
                recette_choisie_id, ingredients_manquants_pour_recette_choisie = self._traiter_menu_standard(
                    date_repas_dt, participants_str, participants_count,
                    used_recipes_current_generation_set, menu_recent_noms,
                    transportable_req, temps_req, nutrition_req
                )
                if recette_choisie_id:
                    nom_plat_final = self.recette_manager.obtenir_nom(recette_choisie_id)
                    if nom_plat_final and "Recette_ID_" not in nom_plat_final:
                        temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
                        used_recipes_current_generation_set.add(recette_choisie_id)

                        for ing_id_manquant, qte_manquante in ingredients_manquants_pour_recette_choisie.items():
                            self.ingredients_a_acheter_cumules[ing_id_manquant] = self.ingredients_a_acheter_cumules.get(ing_id_manquant, 0) + qte_manquante

                        ingredients_consommes_ce_repas = self.recette_manager.decrementer_stock(recette_choisie_id, participants_count, date_repas_dt)

                        if date_repas_dt.weekday() >= 5 and self.recette_manager.est_transportable(recette_choisie_id):
                            plats_transportables_semaine[date_repas_dt] = recette_choisie_id
                        self._log_decision_recette(recette_choisie_id, date_repas_dt, participants_str)
                    else:
                        nom_plat_final = f"Recette ID {recette_choisie_id} - Nom Invalide"
                        remarques_repas = "Erreur: Nom de recette non trouvé pour cet ID."
                        recette_choisie_id = None
                else:
                    nom_plat_final = "Pas de recette trouvée"
                    remarques_repas = "Aucune recette candidate ne correspond aux critères."

            self._ajouter_resultat(resultats_df_list, date_repas_dt, nom_plat_final, participants_str, remarques_repas, temps_prep_final, recette_choisie_id)
            if nom_plat_final and "Pas de recette" not in nom_plat_final and "Pas de reste" not in nom_plat_final and "Erreur" not in nom_plat_final and "Invalide" not in nom_plat_final:
                menu_recent_noms.append(nom_plat_final)
                if len(menu_recent_noms) > 3: menu_recent_noms.pop(0)

            if ingredients_consommes_ce_repas:
                for ing_id_cons in ingredients_consommes_ce_repas:
                    if ing_id_cons and str(ing_id_cons).lower() not in ['nan', 'none', '']:
                        ingredients_effectivement_utilises_ids_set.add(str(ing_id_cons))


        noms_ingredients_utilises_final = sorted(list(filter(None, [
            self.recette_manager.obtenir_nom_ingredient_par_id(ing_id) for ing_id in ingredients_effectivement_utilises_ids_set
        ])))

        df_stock_final_simule = self.recette_manager.stock_simule.copy()
        noms_ingredients_non_utilises_en_stock = []
        if COLONNE_ID_INGREDIENT in df_stock_final_simule.index.name or COLONNE_ID_INGREDIENT in df_stock_final_simule.columns: # Check both
            if COLONNE_ID_INGREDIENT in df_stock_final_simule.columns: # If ID is a column, set it as index temporarily
                df_stock_final_simule_indexed = df_stock_final_simule.set_index(COLONNE_ID_INGREDIENT)
            else: # If already index
                df_stock_final_simule_indexed = df_stock_final_simule

            stock_restant_positif_df = df_stock_final_simule_indexed[df_stock_final_simule_indexed['Qte reste'] > 0]
            ids_stock_restant_positif = set(stock_restant_positif_df.index.astype(str))

            ids_ingredients_non_utilises = ids_stock_restant_positif - ingredients_effectivement_utilises_ids_set
            noms_ingredients_non_utilises_en_stock = sorted(list(filter(None, [
                self.recette_manager.obtenir_nom_ingredient_par_id(ing_id) for ing_id in ids_ingredients_non_utilises
            ])))
        else:
            logger.error(f"'{COLONNE_ID_INGREDIENT}' non trouvé comme index ou colonne dans stock_simule final. Impossible de déterminer les ingrédients non utilisés.")


        liste_courses_finale = []
        for ing_id_achat, qte_achat in self.ingredients_a_acheter_cumules.items():
            nom_ing_achat = self.recette_manager.obtenir_nom_ingredient_par_id(ing_id_achat)
            if nom_ing_achat and "ID_Ing_" not in nom_ing_achat :
                unite_ing = "unité(s)"
                try:
                    # Ici, on accède à df_ingredients_initial qui a COLONNE_ID_INGREDIENT comme colonne
                    unite_data = self.recette_manager.df_ingredients_initial[self.recette_manager.df_ingredients_initial[COLONNE_ID_INGREDIENT].astype(str) == ing_id_achat]['unité']
                    if not unite_data.empty:
                        unite_ing = unite_data.iloc[0]
                except (IndexError, KeyError) as e:
                    logger.warning(f"Unité non trouvée pour l'ingrédient {nom_ing_achat} (ID: {ing_id_achat}) pour la liste de courses. Erreur: {e}")

                liste_courses_finale.append(f"{nom_ing_achat}: {qte_achat:.2f} {unite_ing}")
            else:
                liste_courses_finale.append(f"ID Ingrédient {ing_id_achat}: {qte_achat:.2f} unité(s) (Nom non trouvé)")
        liste_courses_finale.sort()

        logger.info("Génération du menu terminée.")
        return pd.DataFrame(resultats_df_list), noms_ingredients_utilises_final, noms_ingredients_non_utilises_en_stock, liste_courses_finale


# --- Connexion à Notion ---
try:
    NOTION_API_KEY = st.secrets["notion_api_key"]
    DATABASE_ID = st.secrets["notion_database_id"] # ID de votre base de données "Planning Menus"
    notion = Client(auth=NOTION_API_KEY)
except KeyError:
    st.error("Les secrets Notion (notion_api_key ou notion_database_id) ne sont pas configurés. "
             "Veuillez les ajouter dans le fichier .streamlit/secrets.toml ou via l'interface Streamlit Cloud.")
    st.stop()
except Exception as e:
    st.error(f"Erreur lors de l'initialisation du client Notion : {e}")
    st.stop()

# --- Fonctions Notion ---
def query_database(database_id, filter_property=None, filter_value=None):
    """Effectue une requête sur une base de données Notion."""
    try:
        if filter_property and filter_value:
            filter_obj = {
                "property": filter_property,
                "title": { # Assuming filter_property is a title field for page name lookup
                    "equals": filter_value
                }
            }
            results = notion.databases.query(database_id=database_id, filter=filter_obj).get("results")
        else:
            results = notion.databases.query(database_id=database_id).get("results")
        return results
    except Exception as e:
        st.error(f"Erreur lors de la requête Notion sur la base {database_id}: {e}")
        logger.error(f"Erreur lors de la requête Notion sur la base {database_id}: {e}")
        return []

def get_page_properties(page):
    """Extrait les propriétés d'une page Notion."""
    properties = {}
    for prop_name, prop_data in page["properties"].items():
        if prop_data["type"] == "title":
            properties[prop_name] = prop_data["title"][0]["plain_text"] if prop_data["title"] else ""
        elif prop_data["type"] == "rich_text":
            properties[prop_name] = prop_data["rich_text"][0]["plain_text"] if prop_data["rich_text"] else ""
        elif prop_data["type"] == "multi_select":
            properties[prop_name] = [item["name"] for item in prop_data["multi_select"]]
        elif prop_data["type"] == "select":
            properties[prop_name] = prop_data["select"]["name"] if prop_data["select"] else ""
        elif prop_data["type"] == "number":
            properties[prop_name] = prop_data["number"]
        elif prop_data["type"] == "checkbox":
            properties[prop_name] = prop_data["checkbox"]
        elif prop_data["type"] == "date":
            properties[prop_name] = prop_data["date"]["start"] if prop_data["date"] else ""
        elif prop_data["type"] == "url":
            properties[prop_name] = prop_data["url"]
        elif prop_data["type"] == "relation":
            properties[prop_name] = [item["id"] for item in prop_data["relation"]]
        elif prop_data["type"] == "formula":
            if prop_data["formula"]["type"] == "string":
                properties[prop_name] = prop_data["formula"]["string"]
            elif prop_data["formula"]["type"] == "number":
                properties[prop_name] = prop_data["formula"]["number"]
            elif prop_data["formula"]["type"] == "boolean":
                properties[prop_name] = prop_data["formula"]["boolean"]
            elif prop_data["formula"]["type"] == "date":
                properties[prop_name] = prop_data["formula"]["date"]["start"] if prop_data["formula"]["date"] else ""
        else:
            properties[prop_name] = None
    return properties

def create_page(database_id, properties):
    """Crée une nouvelle page dans une base de données Notion."""
    try:
        new_page = notion.pages.create(parent={"database_id": database_id}, properties=properties)
        return new_page
    except Exception as e:
        st.error(f"Erreur lors de la création d'une page dans Notion: {e}")
        logger.error(f"Erreur lors de la création d'une page dans Notion: {e}")
        return None

def update_page_property(page_id, property_name, property_type, value):
    """Met à jour une propriété d'une page Notion."""
    try:
        properties = {}
        if property_type == "rich_text":
            properties[property_name] = {"rich_text": [{"text": {"content": value}}]}
        elif property_type == "date":
            properties[property_name] = {"date": {"start": value}}
        elif property_type == "select":
            properties[property_name] = {"select": {"name": value}}
        elif property_type == "relation":
            properties[property_name] = {"relation": [{"id": item_id} for item_id in value]}
        elif property_type == "number":
            properties[property_name] = {"number": value}
        elif property_type == "checkbox":
            properties[property_name] = {"checkbox": value}
        else:
            st.warning(f"Type de propriété non géré pour la mise à jour : {property_type}")
            return False

        notion.pages.update(page_id=page_id, properties=properties)
        return True
    except Exception as e:
        st.error(f"Erreur lors de la mise à jour de la propriété '{property_name}' de la page {page_id}: {e}")
        logger.error(f"Erreur lors de la mise à jour de la propriété '{property_name}' de la page {page_id}: {e}")
        return False

def get_page_id_by_name(database_id, page_name_property, page_name):
    """Récupère l'ID d'une page Notion par son nom."""
    try:
        results = notion.databases.query(
            database_id=database_id,
            filter={
                "property": page_name_property,
                "title": {
                    "equals": page_name
                }
            }
        ).get("results")
        if results:
            return results[0]["id"]
        return None
    except Exception as e:
        st.error(f"Erreur lors de la recherche de l'ID de la page '{page_name}': {e}")
        logger.error(f"Erreur lors de la recherche de l'ID de la page '{page_name}': {e}")
        return None


def integrate_with_notion(df_menus_complet, database_id):
    """Intègre les menus générés dans une base de données Notion."""
    st.info("Intégration avec Notion en cours...")
    logger.info("Début de l'intégration avec Notion.")

    # Filtrer les lignes qui n'ont pas de recette_nom vide ou "Non défini"
    df_to_integrate = df_menus_complet[
        (df_menus_complet['Nom Plat Final'] != '') &
        (df_menus_complet['Nom Plat Final'] != 'Non défini') &
        (df_menus_complet['Nom Plat Final'] != 'Pas de recette trouvée') &
        (df_menus_complet['Nom Plat Final'] != 'Pas de reste disponible') &
        (~df_menus_complet['Nom Plat Final'].str.contains("Nom Invalide", na=False))
    ].copy()

    if df_to_integrate.empty:
        st.warning("Aucun menu valide à intégrer dans Notion.")
        logger.warning("Aucun menu valide à intégrer dans Notion.")
        return

    # Vérifier l'existence des pages de recettes dans Notion pour récupérer les IDs de relation
    recette_ids = {}
    st.info("Vérification des recettes existantes dans Notion...")
    # Assumer que vous avez une base de données "Recettes" dans Notion avec une propriété "Nom"
    # L'ID de cette base de données "Recettes" pourrait être différente de DATABASE_ID (qui est pour "Planning Menus")
    # Pour simplifier, nous utilisons DATABASE_ID comme base pour les recettes également.
    # Dans un cas réel, vous auriez un NOTION_RECIPES_DATABASE_ID distinct.
    for recette_nom in df_to_integrate['Nom Plat Final'].unique():
        # Utilise la même DATABASE_ID pour rechercher les recettes. Si vos recettes sont dans une autre DB, changez l'ID ici.
        page_id = get_page_id_by_name(DATABASE_ID, COLONNE_NOM, recette_nom) # Assurez-vous que "Nom" est la propriété de titre de votre base de recettes
        if page_id:
            recette_ids[recette_nom] = page_id
        else:
            st.warning(f"La recette '{recette_nom}' n'a pas été trouvée dans Notion. Elle ne sera pas liée.")

    for index, row in df_to_integrate.iterrows():
        date_dt = row['Date'] # C'est déjà un objet datetime
        date_iso = date_dt.isoformat()
        repas_type = row['Type_Repas'] # Déjeuner, Dîner, Petit-déjeuner, Goûter
        recette_nom = row['Nom Plat Final']
        participants = row['Participant(s)']
        recette_id_from_gen = row['Recette ID'] # L'ID de recette générée par votre logique

        # Propriétés de la nouvelle page de menu
        properties = {
            "Date": {
                "date": {
                    "start": date_iso # Convertir en format ISO 8601
                }
            },
            "Repas": {
                "select": {
                    "name": repas_type
                }
            },
            "Nom": { # C'est le titre de la page de menu
                "title": [
                    {
                        "text": {
                            "content": f"{repas_type} - {recette_nom} ({date_dt.strftime('%d/%m/%Y')})"
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

        # Ajouter la relation à la recette si l'ID est trouvé
        if recette_nom in recette_ids:
            properties["Recette"] = { # Assurez-vous que "Recette" est le nom de votre propriété de relation dans la base "Planning Menus"
                "relation": [{"id": recette_ids[recette_nom]}]
            }
        elif recette_id_from_gen: # Si on a l'ID de la recette générée, on peut essayer de la lier directement si elle existe
             properties["Recette"] = {
                "relation": [{"id": recette_id_from_gen}]
            }
        else:
            st.warning(f"Impossible de lier la recette '{recette_nom}' pour le {repas_type} du {date_dt.strftime('%d/%m/%Y')} car elle n'a pas été trouvée dans Notion ou l'ID n'est pas disponible.")


        # Vérifier si la page existe déjà pour éviter les doublons
        # On suppose que le titre "Nom" est unique pour un même jour et repas
        existing_page_id = get_page_id_by_name(DATABASE_ID, "Nom", properties["Nom"]["title"][0]["text"]["content"])

        if existing_page_id:
            st.info(f"La page pour '{repas_type} - {recette_nom} ({date_dt.strftime('%d/%m/%Y')})' existe déjà. Mise à jour en cours...")
            # Si vous souhaitez mettre à jour d'autres propriétés, vous pouvez le faire ici
            # update_page_property(existing_page_id, "Participant(s)", "rich_text", str(participants))
            pass
        else:
            st.info(f"Création de la page pour '{repas_type} - {recette_nom} ({date_dt.strftime('%d/%m/%Y')})'...")
            create_page(DATABASE_ID, properties)
    st.success("Intégration avec Notion terminée.")
    logger.info("Fin de l'intégration avec Notion.")


# --- Application Streamlit principale ---
st.set_page_config(layout="wide", page_title="Générateur de Menus Notion")
st.title("🍽️ Générateur de Menus pour Notion")

st.markdown("""
Cette application vous permet de générer des menus, des listes d'ingrédients,
et de les synchroniser avec votre base de données Notion "Planning Menus".
""")

st.header("1. Téléchargez vos fichiers CSV")
st.warning("Assurez-vous que vos fichiers CSV contiennent les colonnes attendues (voir exemple).")

uploaded_planning = st.file_uploader("Chargez le fichier Planning.csv", type="csv")
uploaded_recettes = st.file_uploader("Chargez le fichier Recettes.csv", type="csv")
uploaded_ingredients = st.file_uploader("Chargez le fichier Ingredients.csv", type="csv")
uploaded_ingredients_recettes = st.file_uploader("Chargez le fichier Ingredients_recettes.csv", type="csv")
uploaded_menus_hist = st.file_uploader("Chargez le fichier Historique_menus.csv (si applicable)", type="csv")


df_planning, df_recettes, df_ingredients, df_ingredients_recettes, df_menus_hist = [None] * 5

if uploaded_planning is not None:
    try:
        df_planning = pd.read_csv(uploaded_planning)
        st.success("Planning.csv chargé avec succès.")
        # Verifier et nettoyer ici
        verifier_colonnes(df_planning, ["Date", "Participants"], "Planning.csv") # 'Date' et 'Participants' sont requis. Les types de repas comme 'Dejeuner'/'Diner' seront dépivotés.
    except Exception as e:
        st.error(f"Erreur de lecture de Planning.csv : {e}")

if uploaded_recettes is not None:
    try:
        df_recettes = pd.read_csv(uploaded_recettes)
        st.success("Recettes.csv chargé avec succès.")
        verifier_colonnes(df_recettes, [COLONNE_ID_RECETTE, COLONNE_NOM, "Calories", COLONNE_TEMPS_TOTAL, COLONNE_AIME_PAS_PRINCIP, "Transportable"], "Recettes.csv")
    except Exception as e:
        st.error(f"Erreur de lecture de Recettes.csv : {e}")

if uploaded_ingredients is not None:
    try:
        df_ingredients = pd.read_csv(uploaded_ingredients)
        st.success("Ingredients.csv chargé avec succès.")
        verifier_colonnes(df_ingredients, [COLONNE_ID_INGREDIENT, "Nom", "Qte reste", "unité"], "Ingredients.csv")
    except Exception as e:
        st.error(f"Erreur de lecture de Ingredients.csv : {e}")

if uploaded_ingredients_recettes is not None:
    try:
        df_ingredients_recettes = pd.read_csv(uploaded_ingredients_recettes)
        st.success("Ingredients_recettes.csv chargé avec succès.")
        verifier_colonnes(df_ingredients_recettes, [COLONNE_ID_RECETTE, "Qté/pers_s", "Ingrédient ok"], "Ingredients_recettes.csv")
    except Exception as e:
        st.error(f"Erreur de lecture de Ingredients_recettes.csv : {e}")

if uploaded_menus_hist is not None:
    try:
        df_menus_hist = pd.read_csv(uploaded_menus_hist)
        st.success("Historique_menus.csv chargé avec succès.")
        verifier_colonnes(df_menus_hist, ["Date", "Recette", "Nom Menu"], "Historique_menus.csv")
    except Exception as e:
        st.error(f"Erreur de lecture de Historique_menus.csv : {e}")
else:
    # Si l'historique n'est pas chargé, initialiser un DataFrame vide
    df_menus_hist = pd.DataFrame(columns=["Date", "Recette", "Nom Menu", "Semaine"])
    st.info("Historique_menus.csv non chargé. La génération du menu se fera sans historique de répétition.")


# Vérifiez si tous les fichiers essentiels sont chargés avant de procéder
if all(df is not None for df in [df_planning, df_recettes, df_ingredients, df_ingredients_recettes]):
    st.header("2. Générer les menus et listes")
    if st.button("Générer les Menus et Listes"):
        with st.spinner("Génération en cours..."):
            try:
                # Initialisation des gestionnaires de données
                recette_manager = RecetteManager(df_recettes, df_ingredients, df_ingredients_recettes)
                menus_history_manager = MenusHistoryManager(df_menus_hist)

                menu_generator = MenuGenerator(
                    menus_history_manager,
                    recette_manager,
                    df_planning # Utilisez le df_planning chargé par Streamlit
                )

                # Générer le menu
                df_menu_genere, ingredients_utilises_menu, ingredients_stock_non_utilises, liste_courses = menu_generator.generer_menu()

                if not df_menu_genere.empty:
                    st.subheader("Aperçu des Menus Générés :")
                    # Assurez-vous que les colonnes affichées correspondent à celles générées
                    st.dataframe(df_menu_genere[['Date', 'Type_Repas', 'Nom Plat Final', 'Participant(s)', 'Remarques', 'Temps Préparation']])

                    # --- Boutons de téléchargement ---
                    # CSV Export
                    df_export_csv = df_menu_genere[['Date', 'Participant(s)', 'Nom Plat Final']].copy()
                    df_export_csv.rename(columns={'Nom Plat Final': 'Nom'}, inplace=True)
                    df_export_csv['Date'] = pd.to_datetime(df_export_csv['Date'], errors='coerce').dt.strftime('%Y-%m-%d %H:%M')
                    csv_buffer = io.StringIO()
                    df_export_csv.to_csv(csv_buffer, index=False, encoding="utf-8-sig")
                    csv_data = csv_buffer.getvalue().encode("utf-8-sig")
                    st.download_button(
                        label="Télécharger Menus_generes.csv",
                        data=csv_data,
                        file_name=FICHIER_SORTIE_MENU_CSV,
                        mime="text/csv",
                    )

                    # Text Summary
                    contenu_fichier_recap_txt = []
                    if ingredients_utilises_menu:
                        titre_utilises = "\nIngrédients en stock effectivement utilisés dans ce menu :"
                        st.subheader(titre_utilises)
                        contenu_fichier_recap_txt.append(titre_utilises.strip() + "\n")
                        for nom_ing in ingredients_utilises_menu:
                            line = f"- {nom_ing}"
                            st.write(line)
                            contenu_fichier_recap_txt.append(line + "\n")
                    else:
                        message_aucun_utilises = "\nAucun ingrédient du stock n'a été effectivement utilisé pour ce menu."
                        st.write(message_aucun_utilises)
                        contenu_fichier_recap_txt.append(message_aucun_utilises.strip() + "\n")

                    if ingredients_stock_non_utilises:
                        titre_non_utilises = "\nIngrédients encore en stock (Qte > 0) et non utilisés dans ce menu :"
                        st.subheader(titre_non_utilises)
                        contenu_fichier_recap_txt.append("\n" + titre_non_utilises.strip() + "\n")
                        for nom_ing in ingredients_stock_non_utilises:
                            line = f"- {nom_ing}"
                            st.write(line)
                            contenu_fichier_recap_txt.append(line + "\n")
                    else:
                        message_tous_utilises = "\nTous les ingrédients en stock ont été utilisés ou aucun ingrédient avec Qte > 0 n'est resté."
                        st.write(message_tous_utilises)
                        contenu_fichier_recap_txt.append("\n" + message_tous_utilises.strip() + "\n")

                    if liste_courses:
                        titre_courses = "\nListe de courses (ingrédients manquants pour le menu) :"
                        st.subheader(titre_courses)
                        contenu_fichier_recap_txt.append("\n" + titre_courses.strip() + "\n")
                        for item_course in liste_courses:
                            line = f"- {item_course}"
                            st.write(line)
                            contenu_fichier_recap_txt.append(line + "\n")
                    else:
                        message_aucune_course = "\nAucun ingrédient à acheter pour ce menu (tout est en stock ou aucune recette planifiée)."
                        st.write(message_aucune_course)
                        contenu_fichier_recap_txt.append("\n" + message_aucune_course.strip() + "\n")

                    txt_buffer = io.StringIO()
                    txt_buffer.writelines(contenu_fichier_recap_txt)
                    txt_data = txt_buffer.getvalue().encode("utf-8")
                    st.download_button(
                        label="Télécharger Liste_ingredients.txt",
                        data=txt_data,
                        file_name=FICHIER_SORTIE_LISTES_TXT,
                        mime="text/plain",
                    )

                    st.success("Génération des menus et listes terminée.")

                    st.header("3. Intégrer avec Notion")
                    notion_integrate = st.checkbox("Envoyer les menus générés à Notion?")
                    if notion_integrate:
                        if st.button("Lancer l'intégration Notion"):
                            with st.spinner("Intégration Notion en cours..."):
                                # Renommer les colonnes pour qu'elles correspondent aux attentes de integrate_with_notion
                                # (qui attend 'recette_nom' et 'repas_type')
                                df_for_notion_integration = df_menu_genere.rename(
                                    columns={'Nom Plat Final': 'recette_nom', 'Type_Repas': 'repas_type'}
                                )
                                integrate_with_notion(df_for_notion_integration, DATABASE_ID)
                                st.success("Processus d'intégration Notion terminé.")
                else:
                    st.warning("Aucun menu n'a pu être généré. Veuillez vérifier vos fichiers.")
            except Exception as e:
                st.error(f"Une erreur est survenue lors de la génération du menu : {e}")
                logger.error(f"Erreur majeure dans la génération du menu: {e}", exc_info=True)
else:
    st.warning("Veuillez charger tous les fichiers CSV nécessaires pour activer la génération (Planning, Recettes, Ingredients, Ingredients_recettes).")

st.info("N'oubliez pas de configurer vos secrets Notion dans Streamlit Cloud ou dans votre fichier `.streamlit/secrets.toml`.")
