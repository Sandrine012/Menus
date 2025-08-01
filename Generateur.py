import streamlit as st
import pandas as pd
import io
from datetime import datetime

# --------------------------------------------------
# 1.  Fonctions utilitaires d’export 4-en-1
# --------------------------------------------------
def build_csv_bytes(df: pd.DataFrame) -> bytes:
    """Convertit un DataFrame en bytes UTF-8-SIG pour download_button."""
    buffer = io.StringIO()
    df.to_csv(buffer, index=False, encoding="utf-8-sig")
    return buffer.getvalue().encode("utf-8-sig")


def pack_four_files(df_menus, df_ingredients, df_ingredients_recettes, df_recettes):
    """
    Retourne un dict {nom_fichier: bytes} prêt pour zip ou multi-download.
    Les noms correspondent strictement aux fichiers fournis par l’utilisateur.
    """
    fichiers = {
        "Menus_generes.csv":          build_csv_bytes(df_menus),
        "Liste_ingredients.txt":      build_courses_txt(df_ingredients, df_ingredients_recettes).encode("utf-8"),
        "Recettes.csv":               build_csv_bytes(df_recettes),
        "Ingredients_recettes.csv":   build_csv_bytes(df_ingredients_recettes),
    }
    return fichiers


def build_courses_txt(df_ing_stock, df_ing_recette) -> str:
    """
    Recalcule rapidement la liste de courses (logique identique à votre code
    d’origine) et renvoie une chaîne prête à être écrite dans le .txt.
    """
    # agrégation quantités
    df_tmp = (df_ing_recette
              .groupby(['Ingrédient ok'])['Qté/pers_s']
              .sum()
              .reset_index()
              .merge(df_ing_stock[['Nom', 'Qte reste']], left_on='Ingrédient ok',
                     right_on='Nom', how='left'))
    df_tmp['Qte reste'] = pd.to_numeric(df_tmp['Qte reste'], errors='coerce').fillna(0)
    df_tmp['A acheter'] = (df_tmp['Qté/pers_s'] - df_tmp['Qte reste']).clip(lower=0)

    lignes = ["Liste de courses (éléments à acheter) :\n"]
    for _, row in df_tmp[df_tmp['A acheter'] > 0].iterrows():
        lignes.append(f"- {row['A acheter']:.2f}  de  {row['Ingrédient ok']}\n")
    return "".join(lignes)


# --------------------------------------------------
# 2.  Intégration dans l’appli Streamlit existante
# --------------------------------------------------
def zone_telechargements():
    st.header("📥 Télécharger les 4 fichiers en une seule action")

    # Vérifie que les 4 DataFrame sont bien prêts
    required_keys = ['df_menus_gen', 'df_ingredients', 'df_ingredients_recettes', 'df_recettes']
    if not all(k in st.session_state and not st.session_state[k].empty for k in required_keys):
        st.info("Les données ne sont pas encore prêtes – générez-les d’abord.")
        return

    # Construction des fichiers
    fichiers = pack_four_files(
        st.session_state['df_menus_gen'],
        st.session_state['df_ingredients'],
        st.session_state['df_ingredients_recettes'],
        st.session_state['df_recettes']
    )

    # Un bouton => quatre téléchargements synchrones
    if st.button("💾 Télécharger les 4 CSV"):
        for nom, contenu in fichiers.items():
            st.download_button(
                label=f"Télécharger {nom}",
                data=contenu,
                file_name=nom,
                mime="text/csv" if nom.endswith(".csv") else "text/plain",
                key=nom  # clé unique par fichier
            )
        st.success("Téléchargement prêt ! Cliquez sur chaque lien généré ci-dessous.")

# --------------------------------------------------
# 3.  Ajout dans votre main()
# --------------------------------------------------
def main():
    # ... tout votre code existant ...

    # Après la génération de df_menus_complet  ➜ stocke dans session_state
    if 'df_menus_gen' not in st.session_state and not df_menus_complet.empty:
        st.session_state['df_menus_gen'] = df_menus_complet[['Date', 'Participant(s)', 'Nom']]

    # Place la nouvelle zone de téléchargement à la fin
    zone_telechargements()

    # ... fin main ...

if __name__ == "__main__":
    main()
