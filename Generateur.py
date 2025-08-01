import streamlit as st
import pandas as pd
import time, logging, httpx
from datetime import datetime
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError

# ────────────── LOGGING ──────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ─────────── SECRETS NOTION ───────────
NOTION_API_KEY           = st.secrets["notion_api_key"]
ID_RECETTES              = st.secrets["notion_database_id_recettes"]
ID_MENUS                 = st.secrets["notion_database_id_menus"]
ID_INGREDIENTS           = st.secrets["notion_database_id_ingredients"]
ID_INGREDIENTS_RECETTES  = st.secrets["notion_database_id_ingredients_recettes"]

notion = Client(auth=NOTION_API_KEY)

# ─────────── CONSTANTES CSV ───────────
CSV_RECETTES             = "Recettes.csv"
CSV_MENUS                = "Menus.csv"
CSV_INGREDIENTS          = "Ingredients.csv"
CSV_INGREDIENTS_RECETTES = "Ingredients_recettes.csv"

BATCH, RETRY, WAIT = 50, 3, 5
SAISON_FILTRE = "Printemps"

# ──────────── FONCTION GÉNÉRIQUE ────────────
def paginate(db_id, **kwargs):
    out, start, r = [], None, 0
    while True:
        try:
            resp = notion.databases.query(
                database_id=db_id,
                start_cursor=start,
                page_size=BATCH,
                **kwargs)
            out.extend(resp["results"])
            if not resp["has_more"]:
                break
            start = resp["next_cursor"]; time.sleep(0.3); r = 0
        except (RequestTimeoutError, httpx.TimeoutException, httpx.ReadTimeout):
            r += 1
            if r > RETRY:
                st.error("Timeout répété – abandon."); break
            time.sleep(WAIT * r)
        except APIResponseError as e:
            st.error(f"Erreur API Notion : {e}"); break
    return out

# ──────────── RECETTES ────────────
HDR_REC = ["Page_ID","Nom","ID_Recette","Saison",
           "Calories","Proteines","Temps_total",
           "Aime_pas_princip","Type_plat","Transportable"]
MAP = {
    "Nom":("Nom_plat","title"),"ID_Recette":("ID_Recette","uid"),
    "Saison":("Saison","ms"),"Calories":("Calories Recette","roll"),
    "Proteines":("Proteines Recette","roll"),"Temps_total":("Temps_total","form"),
    "Aime_pas_princip":("Aime_pas_princip","rollstr"),
    "Type_plat":("Type_plat","ms"),"Transportable":("Transportable","selcb")
}
def pv(prop, kind):
    if not prop:return ""
    t=prop.get("type")
    if kind=="title":      return "".join(x["plain_text"] for x in prop["title"])
    if kind=="uid":        uid=prop["unique_id"]; p,n=uid.get("prefix"),uid.get("number"); return f"{p}-{n}" if p else str(n or "")
    if kind=="ms":         return ", ".join(o["name"] for o in prop["multi_select"])
    if kind=="roll":       return str(prop["rollup"].get("number") or "")
    if kind=="form":       fo=prop["formula"]; return str(fo.get("number") or fo.get("string") or "")
    if kind=="rollstr":    return ", ".join(it["formula"].get("string") or "." for it in prop["rollup"]["array"])
    if kind=="selcb":      return "Oui" if (t=="select" and (prop["select"] or {}).get("name","").lower()=="oui") or (t=="checkbox" and prop["checkbox"]) else ""
    return ""
def extract_recettes():
    filt={"and":[
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
    for p in paginate(ID_RECETTES,filter=filt):
        pr=p["properties"]; row=[p["id"]]
        for col in HDR_REC[1:]:
            key,kind=MAP[col]; row.append(pv(pr.get(key),kind))
        rows.append(row)
    return pd.DataFrame(rows,columns=HDR_REC)

# ──────────── MENUS ────────────
HDR_MENUS=["Nom Menu","Recette","Date"]
def extract_menus():
    rows=[]
    for p in paginate(ID_MENUS,
            filter={"property":"Recette","relation":{"is_not_empty":True}}):
        pr=p["properties"]
        nom="".join(t["plain_text"] for t in pr["Nom Menu"]["title"])
        # recettes
        ids=[]
        rel=pr["Recette"]
        if rel["type"]=="relation":
            ids=[r["id"] for r in rel["relation"]]
        else:
            for it in rel["rollup"]["array"]:
                ids.extend([it.get("id")] if it.get("id") else [r["id"] for r in it.get("relation",[])])
        # date
        d=""
        if pr["Date"]["date"] and pr["Date"]["date"]["start"]:
            d=datetime.fromisoformat(pr["Date"]["date"]["start"].replace("Z","+00:00")).strftime("%Y-%m-%d")
        rows.append([nom.strip(),", ".join(ids),d])
    return pd.DataFrame(rows,columns=HDR_MENUS)

# ──────────── INGREDIENTS ────────────
HDR_INGR=["Page_ID","Nom","Type de stock","unité","Qte reste"]
def extr_ingr():
    rows=[]
    for p in paginate(ID_INGREDIENTS,
            filter={"property":"Type de stock","select":{"equals":"Autre type"}}):
        pr=p["properties"]
        unite_prop=pr.get("unité",{})
        if unite_prop.get("type")=="rich_text":
            unite="".join(t["plain_text"] for t in unite_prop["rich_text"])
        elif unite_prop.get("type")=="select":
            unite=(unite_prop["select"] or {}).get("name","")
        else:
            unite=""
        rows.append([
            p["id"],
            "".join(t["plain_text"] for t in pr["Nom"]["title"]),
            (pr["Type de stock"]["select"] or {}).get("name",""),
            unite,
            str(pr["Qte reste"]["number"] or "")
        ])
    return pd.DataFrame(rows,columns=HDR_INGR)

# ──────────── INGREDIENTS ↔ RECETTES ────────────
HDR_IR=["Page_ID","Qté/pers_s","Ingrédient ok","Type de stock f"]
def extr_ingr_rec():
    rows=[]
    for p in paginate(ID_INGREDIENTS_RECETTES,
            filter={"property":"Type de stock f","formula":{"string":{"equals":"Autre type"}}}):
        pr=p["properties"]
        # parent
        parent=pr.get("Elément parent",{})
        pid=""
        if parent and parent["type"]=="relation" and parent["relation"]:
            pid=parent["relation"][0]["id"]
        if not pid: pid=p["id"]
        qte=pr["Qté/pers_s"]["number"]
        if qte and qte>0:
            rows.append([
                pid,
                str(qte),
                ", ".join(r["id"] for r in pr["Ingrédient ok"]["relation"]),
                pr["Type de stock f"]["formula"]["string"] or ""
            ])
    return pd.DataFrame(rows,columns=HDR_IR)

# ──────────── UI ────────────
st.set_page_config(page_title="Exports Notion (4 CSV)", layout="centered")
st.title("📋 Exports Notion : Recettes • Menus • Ingrédients • Liens")

def show(btn_label, func, csv_name):
    if st.button(btn_label):
        with st.spinner("Extraction en cours…"):
            df=func()
        if df.empty:
            st.error("Aucune ligne trouvée (vérifiez ID & droits).")
        else:
            st.success(f"{len(df)} lignes extraites.")
            st.dataframe(df, use_container_width=True)
            st.download_button("📥 "+csv_name,
                df.to_csv(index=False,encoding="utf-8-sig").encode("utf-8-sig"),
                file_name=csv_name, mime="text/csv")

show("Extraire les recettes",             extract_recettes,          CSV_RECETTES)
st.divider()
show("Extraire les menus",                extract_menus,             CSV_MENUS)
st.divider()
show("Extraire les ingrédients",          extr_ingr,                 CSV_INGREDIENTS)
st.divider()
show("Extraire ingrédients-recettes",     extr_ingr_rec,             CSV_INGREDIENTS_RECETTES)

st.info("Chaque bouton interroge uniquement la base concernée et génère un CSV identique à votre modèle (UTF-8-SIG).")
