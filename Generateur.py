import streamlit as st
import pandas as pd
import io

# ─────────────────────────────────────────────
# 1. Utilitaire : convertir un DataFrame en CSV
# ─────────────────────────────────────────────
def _csv_bytes(df: pd.DataFrame) -> bytes:
    """
    Renvoie le contenu CSV (UTF-8 avec BOM) d’un DataFrame,
    prêt à être utilisé dans st.download_button().
    """
    buff = io.StringIO()
    df.to_csv(buff, index=False, encoding="utf-8-sig")
    return buff.getvalue().encode("utf-8-sig")


# ─────────────────────────────────────────────
# 2. Bloc Export : 1 clic ➜ 4 liens CSV
# ─────────────────────────────────────────────
def bloc_export() -> None:
    """Affiche le bouton ‘💾 Préparer les fichiers’ puis les 4 liens CSV."""
    st.subheader("📥 Export CSV (4 fichiers)")

    # Les quatre DataFrame attendus dans st.session_state
    keys = [
        "df_menus_gen",               # DataFrame des menus générés
        "df_ingredients",             # Ingrédients en stock
        "df_ingredients_recettes",    # Ingrédients par recette
        "df_recettes",                # Recettes
    ]

    # Vérifie que chaque DataFrame existe et contient des lignes
    if not all(
        k in st.session_state
        and isinstance(st.session_state[k], pd.DataFrame)
        and not st.session_state[k].empty
        for k in keys
    ):
        st.info("Les 4 DataFrame ne sont pas encore prêts.")
        return

    # Un seul bouton pour préparer les 4 liens
    if st.button("💾 Préparer les fichiers à télécharger"):
        fichiers = {
            "Menus_generes.csv":        _csv_bytes(st.session_state["df_menus_gen"]),
            "Ingredients.csv":          _csv_bytes(st.session_state["df_ingredients"]),
            "Ingredients_recettes.csv": _csv_bytes(st.session_state["df_ingredients_recettes"]),
            "Recettes.csv":             _csv_bytes(st.session_state["df_recettes"]),
        }

        # Génère un lien de téléchargement par fichier
        for nom, contenu in fichiers.items():
            st.download_button(
                label=f"Télécharger {nom}",
                data=contenu,
                file_name=nom,
                mime="text/csv",
                key=nom,            # clé unique par bouton
            )
        st.success("Fichiers prêts ! Clique sur chaque lien pour les récupérer.")


# ─────────────────────────────────────────────
# 3. Intégration dans ton main() existant
# ─────────────────────────────────────────────
def main():
    st.title("🍽️ Générateur de Menus Notion")

    # ─── Ton pipeline habituel : chargement Notion, compteurs, etc. ───
    # Exemple (déjà présent dans ton code) :
    #
    # df_recettes = get_recettes_data()
    # st.session_state["df_recettes"] = df_recettes
    #
    # df_ingredients = get_ingredients_data()
    # st.session_state["df_ingredients"] = df_ingredients
    #
    # df_ing_recettes = get_ingredients_recettes_data()
    # st.session_state["df_ingredients_recettes"] = df_ing_recettes
    #
    # df_menus_complet = construire_menus(df_recettes)  # ta logique
    # st.session_state["df_menus_gen"] = df_menus_complet[["Date", "Participant(s)", "Nom"]]
    #
    # st.write(f"Recettes : {len(df_recettes)} lignes")  # tes compteurs
    # st.write(f"Ingrédients : {len(df_ingredients)} lignes")
    # ...

    # ─── Nouveau bloc export ──────────────────────────────────────────
    bloc_export()

    # ─── Reste de l’app (inchangé) ───────────────────────────────────
    # ...


if __name__ == "__main__":
    main()
