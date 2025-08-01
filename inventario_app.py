"""
App de gestión de inventario de licores para un bar/restaurante.

Esta aplicación de Streamlit proporciona un conjunto completo de módulos
para registrar entradas, salidas y transferencias de productos,
mantenimiento de recetas de tragos, cálculo de stock por ubicación y
auditorías diarias/semanales. Todos los datos se sincronizan
bidireccionalmente con Google Sheets, permitiendo que el inventario se
gestione tanto desde la app como desde las hojas de cálculo.

Las tablas de Google Sheets se crean de manera automática si no
existen, y se actualizan tras cada operación. La app utiliza
``st.session_state`` para conservar en memoria los dataframes
mientras se navega entre pestañas.

Para utilizar la app:
  1. Coloque en la carpeta del proyecto un archivo ``credenciales.json``
     con las credenciales de un servicio de Google autorizado a
     editar el spreadsheet.
  2. Ajuste el nombre de ``SPREADSHEET_NAME`` según desee.
  3. Ejecute ``streamlit run inventario_app.py``.

El objetivo de este archivo es servir como ejemplo de una solución
completa que cubra la mayoría de requerimientos descritos en el
enunciado. Puede ampliarse según las necesidades concretas de cada
establecimiento.
"""

import streamlit as st
import pandas as pd
from datetime import datetime, date, timedelta
import plotly.express as px
import gspread
from google.oauth2.service_account import Credentials


# ============================
# Configuración de la página
# ============================
st.set_page_config(page_title="Inventario de Licores", layout="wide")


# =====================================================
# Conexión a Google Sheets y utilidades de sincronización
# =====================================================
SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
# Nombre del spreadsheet donde se guardarán los datos.
SPREADSHEET_NAME = "Inventario_Licores"
# Ruta al archivo de credenciales JSON. Debe existir en el directorio
# de trabajo. Para producir uno nuevo consulte la documentación de
# Google Cloud.
CREDENTIALS_PATH = "credenciales.json"


@st.cache_resource(show_spinner=False)
def conectar_google_sheets():
    """Autentica y devuelve una instancia del spreadsheet.

    Se utiliza ``st.cache_resource`` para evitar que la conexión se
    establezca repetidamente en cada recarga. Si las credenciales o
    parámetros cambian, reinicie la aplicación (``Clear Cache``) para
    que se reconecte.
    """
    # Antes hacía esto:
    # creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPE)

    # Ahora cargamos las credenciales desde los secretos de Streamlit:
    creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=SCOPE)

    client = gspread.authorize(creds)
    sheet = client.open(SPREADSHEET_NAME)
    return sheet


def exportar_a_google_sheets(nombre_pestana: str, df: pd.DataFrame) -> None:
    """Exporta un dataframe a una pestaña del spreadsheet.

    Si la pestaña no existe se crea. Antes de escribir se borra su
    contenido para evitar duplicados. Convierte todas las columnas a
    texto para que la API de Sheets no cambie tipos inesperadamente.
    """
    try:
        sheet = conectar_google_sheets()
        # Crear hoja si no existe
        if nombre_pestana not in [ws.title for ws in sheet.worksheets()]:
            sheet.add_worksheet(title=nombre_pestana, rows=2000, cols=50)
        ws = sheet.worksheet(nombre_pestana)
        ws.clear()
        # Convertir a string
        df_str = df.copy().astype(str)
        # Escribir cabecera
        ws.append_row(list(df_str.columns))
        # Escribir filas
        for row in df_str.itertuples(index=False):
            ws.append_row(list(row))
    except Exception as e:
        st.error(f"Error exportando a Sheets: {e}")


def importar_de_google_sheets(nombre_pestana: str) -> pd.DataFrame:
    """Lee una pestaña del spreadsheet y la devuelve como DataFrame.

    Si la pestaña no existe o está vacía, devuelve un DataFrame vacío
    con cero filas y cero columnas. La función está envuelta en un
    ``try`` para capturar posibles errores de autenticación o
    conectividad.
    """
    try:
        sheet = conectar_google_sheets()
        ws = sheet.worksheet(nombre_pestana)
        data = ws.get_all_records()
        return pd.DataFrame(data)
    except Exception:
        return pd.DataFrame()


def inicializar_dataframe_en_estado(nombre: str, columnas: list[str]) -> None:
    """Asegura que un DataFrame está presente en ``st.session_state``.

    Si ``st.session_state[nombre]`` ya existe se deja intacto. En caso
    contrario se importa desde Google Sheets y, si el resultado está
    vacío, se crea un DataFrame con las columnas indicadas para
    comenzar con estructura conocida.
    """
    if nombre not in st.session_state:
        df = importar_de_google_sheets(nombre)
        # Si la hoja está vacía y se han indicado columnas, crear un dataframe con dichas columnas
        if df.empty and columnas:
            # Si la hoja está vacía crear un dataframe con las columnas deseadas
            df = pd.DataFrame(columns=columnas)
            try:
                exportar_a_google_sheets(nombre, df)
            except Exception:
                pass
        else:
            # Asegurar que existan todas las columnas definidas. Si faltan
            # columnas nuevas (por ejemplo, Turno en Auditoría), se añaden
            # con valores nulos para mantener la compatibilidad.
            for col in columnas:
                if col not in df.columns:
                    df[col] = None
            # Conservar el orden de columnas especificado
            df = df[columnas]
        st.session_state[nombre] = df


def actualizar_inventario_registro(df_inventario: pd.DataFrame, registro: pd.DataFrame) -> pd.DataFrame:
    """Concatena un registro al inventario y devuelve la versión actualizada.

    La función asume que el inventario se guarda en el estado con
    nombre ``inventario``. Tras concatenar también actualiza la hoja
    "Inventario" de Google Sheets. La columna ``Fecha`` debe ser
    datetime o string; se convertirá a ISO formato para persistir.
    """
    inventario_actual = st.session_state.get("Inventario", pd.DataFrame())
    inv_nuevo = pd.concat([inventario_actual, registro], ignore_index=True)
    st.session_state["Inventario"] = inv_nuevo
    # Convertir fechas a string ISO
    inv_export = inv_nuevo.copy()
    if "Fecha" in inv_export.columns:
        inv_export["Fecha"] = inv_export["Fecha"].astype(str)
    exportar_a_google_sheets("Inventario", inv_export)
    return inv_nuevo


def calcular_stock(inventario: pd.DataFrame) -> pd.DataFrame:
    """Calcula el stock teórico por producto y ubicación.

    Agrupa por ``Producto`` y ``Ubicación`` sumando la columna
    ``Cantidad``. Si el inventario está vacío, devuelve un DataFrame
    vacío con las columnas correspondientes.
    """
    if inventario.empty:
        return pd.DataFrame(columns=["Producto", "Ubicación", "Stock"])
    stock = (
        inventario.groupby(["Producto", "Ubicación"], as_index=False)["Cantidad"]
        .sum()
        .rename(columns={"Cantidad": "Stock"})
    )
    return stock


def obtener_intervalo_fechas(periodo: str) -> tuple[date, date]:
    """Devuelve el rango de fechas para reportes rápidos.

    ``periodo`` puede ser "Hoy", "Última semana", "Último mes" o
    "Personalizado". Para la opción personalizada la función devuelve
    (None, None) y se debe solicitar al usuario que seleccione las
    fechas manualmente.
    """
    hoy = date.today()
    if periodo == "Hoy":
        return hoy, hoy
    elif periodo == "Última semana":
        inicio = hoy - timedelta(days=6)
        return inicio, hoy
    elif periodo == "Último mes":
        inicio = hoy - timedelta(days=29)
        return inicio, hoy
    else:
        return None, None


# ============================
# Usuarios y roles
# ============================
USUARIOS = {
    "bar1": {"clave": "clave123", "rol": "bartender"},
    "almacen": {"clave": "almacen1", "rol": "almacenista"},
    "gerente": {"clave": "admin999", "rol": "admin"},
    # El usuario supervisor/monitor tiene permisos de solo lectura para revisar
    # métricas y reportes sin modificar los datos.
    "supervisor": {"clave": "super123", "rol": "supervisor"},
}

UBICACIONES = ["Almacén", "Bar", "Vinera"]


def usuario_con_acceso(rol_requerido: list[str]) -> bool:
    """Verifica si el rol del usuario actual está en la lista indicada.

    Si el rol requerido no se cumple, muestra una advertencia y retorna
    ``False``. Esta función facilita la protección de secciones de la
    interfaz según el rol (p. ej. solo el almacén puede registrar
    entradas y transferencias).
    """
    rol = st.session_state.get("rol", "")
    if rol not in rol_requerido:
        st.warning(
            f"Acceso restringido. Esta sección está disponible para roles: {', '.join(rol_requerido)}"
        )
        return False
    return True


# ============================
# Carga inicial de dataframes
# ============================
# Al iniciar la app se cargan las diferentes hojas. Se definen aquí
# las columnas esperadas para cada hoja, de manera que si está vacía
# se cree con la estructura apropiada.
# Definición de columnas por hoja
#
# Se añade la columna ``Turno`` en las auditorías diarias y en el
# registro de stock físico para distinguir entre las dos cargas
# diarias (apertura y cierre). De este modo se pueden almacenar y
# consultar varias auditorías en un mismo día. La columna Turno
# tomará valores como ``Apertura`` o ``Cierre``.
hojas_y_columnas = {
    "Catalogo": ["Nombre", "Tipo", "ML", "Stock Min"],
    "Inventario": ["Fecha", "Tipo", "Producto", "Cantidad", "Ubicación", "Usuario"],
    "Entradas": ["Fecha", "Producto", "Cantidad", "Usuario", "Ubicación"],
    "Salidas": ["Fecha", "Producto/Trago", "Cantidad", "Usuario", "Ubicación", "Tipo"],
    "Transferencias": ["Fecha", "Producto", "Cantidad", "Origen", "Destino", "Usuario"],
    # Las devoluciones registran tanto el origen como el destino para
    # comprender mejor el flujo de producto. La cantidad se registra
    # siempre como positiva en la columna "Cantidad" para la hoja de
    # devoluciones; sin embargo, en el inventario se realizan dos
    # movimientos (negativo en origen y positivo en destino).
    "Devoluciones": [
        "Fecha",
        "Producto",
        "Cantidad",
        "Origen",
        "Destino",
        "Usuario",
        "Motivo",
    ],
    "Recetas": ["Trago", "Ingrediente", "Cantidad_ml"],
    # En StockFisico registramos la cantidad física diaria. Se agrega la
    # columna Turno para distinguir entre las auditorías de apertura y
    # cierre.
    "StockFisico": ["Fecha", "Producto", "Ubicación", "Turno", "Stock_Fisico"],
    # En Auditoria_Diaria registramos tanto el stock teórico como el
    # stock físico y la diferencia. Se incorpora Turno.
    "Auditoria_Diaria": ["Fecha", "Producto", "Ubicación", "Turno", "Stock_Teorico", "Stock_Fisico", "Diferencia"],
    "Auditoria_Semanal": ["Semana", "Producto", "Ubicación", "Diferencia_Acumulada"],
    # Consumos registra el detalle del consumo de ingredientes al servir tragos.
    "Consumos": ["Fecha", "Trago", "Ingrediente", "Cantidad_Usada", "Ubicación", "Usuario"],
}

# Inicializar dataframes en el estado
for hoja, columnas in hojas_y_columnas.items():
    inicializar_dataframe_en_estado(hoja, columnas)


# ============================
# Sidebar de autenticación
# ============================
with st.sidebar:
    st.title("🔐 Acceso por usuario")
    # Mostrar la lista de usuarios y seleccionar por defecto el gerente para
    # facilitar el acceso a quienes administran la aplicación. El índice se
    # determina buscando la posición de "gerente" en la lista de claves.
    usuarios_lista = list(USUARIOS.keys())
    idx_default = usuarios_lista.index("gerente") if "gerente" in usuarios_lista else 0
    usuario = st.selectbox("Usuario", usuarios_lista, index=idx_default)
    clave_ingresada = st.text_input("Contraseña", type="password")
    # Verificar la contraseña
    if clave_ingresada != USUARIOS[usuario]["clave"]:
        st.warning("Clave incorrecta")
        st.stop()
    # Guardar rol en session_state
    st.session_state["rol"] = USUARIOS[usuario]["rol"]
    rol = st.session_state["rol"]
    st.info(f"Has iniciado sesión como: {usuario} ({rol})")
    # Botón para actualizar los datos manualmente desde Google Sheets
    if st.button("🔄 Actualizar datos"):
        # Recargar todos los dataframes en session_state
        for hoja, columnas in hojas_y_columnas.items():
            df = importar_de_google_sheets(hoja)
            # Si está vacío pero hay columnas definidas, crear estructura
            if df.empty and columnas:
                df = pd.DataFrame(columns=columnas)
            st.session_state[hoja] = df
        st.success("Datos actualizados desde Google Sheets.")
        st.rerun()


# ============================
# Interfaz principal con pestañas dinámicas
# ============================
st.title("🍸 Sistema de Inventario de Licores")

# Definición de los módulos con su nombre visible y clave interna.
# Definición de los módulos con su nombre visible y clave interna.
# El orden de este listado determina el orden de las pestañas en la
# interfaz. Se ha priorizado colocar primero un panel general y el
# stock, seguidos de las operaciones más comunes (salidas, entradas,
# transferencias y devoluciones), luego las recetas y finalmente las
# auditorías e historial. Esta disposición facilita que al abrir la
# aplicación el usuario vea de inmediato un resumen del inventario
# disponible y el estado de los productos.
modules_info = [
    {"name": "Panel", "internal": "panel"},
    {"name": "Stock", "internal": "stock"},
    {"name": "Salidas", "internal": "salidas"},
    {"name": "Entradas", "internal": "entradas"},
    {"name": "Transferencias", "internal": "transferencias"},
    {"name": "Devoluciones", "internal": "devoluciones"},
    {"name": "Recetas", "internal": "recetas"},
    {"name": "Auditoría Diaria", "internal": "auditoria_diaria"},
    {"name": "Auditoría Semanal", "internal": "auditoria_semanal"},
    {"name": "Historial", "internal": "historial"},
    {"name": "Catálogo", "internal": "catalogo"},
]

# Mapeo de módulos permitidos por rol. Los nombres internos determinan
# qué pestañas aparecen en la interfaz para cada usuario.
allowed_tabs_by_role = {
    "bartender": ["panel", "salidas", "devoluciones", "stock", "historial", "recetas"],
    "almacenista": [mod["internal"] for mod in modules_info],
    "admin": [mod["internal"] for mod in modules_info],
    "supervisor": ["panel", "stock", "auditoria_diaria", "auditoria_semanal", "historial"],
}

rol_actual = st.session_state.get("rol", "")
visible_internal = [
    mod["internal"]
    for mod in modules_info
    if mod["internal"] in allowed_tabs_by_role.get(rol_actual, [])
]
visible_names = [
    mod["name"]
    for mod in modules_info
    if mod["internal"] in allowed_tabs_by_role.get(rol_actual, [])
]

tabs_rendered = st.tabs(visible_names)
tab_dict = {
    internal: tabs_rendered[i]
    for i, internal in enumerate(visible_internal)
}

# ============================
# Módulo: Panel de control
# ============================
if "panel" in tab_dict:
    with tab_dict["panel"]:
        st.subheader("📊 Panel de Resumen")
        inventario_df = st.session_state["Inventario"]
        catalogo_df = st.session_state["Catalogo"]
        if inventario_df.empty:
            st.info("Aún no se han registrado movimientos de inventario.")
        else:
            # Calcular stock teórico por producto y ubicación
            stock_df = calcular_stock(inventario_df)
            # Determinar el estado de cada producto usando el stock mínimo.
            estados = []
            for _, row in stock_df.iterrows():
                prod = row["Producto"]
                min_vals = catalogo_df.loc[catalogo_df["Nombre"] == prod, "Stock Min"].values
                min_val = min_vals[0] if len(min_vals) > 0 else 0
                try:
                    min_val_f = float(min_val)
                except (ValueError, TypeError):
                    min_val_f = 0.0
                if min_val_f == 0:
                    estados.append("Sin mínimo")
                elif row["Stock"] < min_val_f:
                    estados.append("Crítico")
                elif row["Stock"] < min_val_f * 2:
                    estados.append("Bajo")
                else:
                    estados.append("Suficiente")
            stock_df = stock_df.copy()
            stock_df["Estado"] = estados
            # Contar productos por estado
            conteo_estados = stock_df["Estado"].value_counts().to_dict()
            total_items = len(stock_df)
            criticos = conteo_estados.get("Crítico", 0)
            bajos = conteo_estados.get("Bajo", 0)
            suficientes = conteo_estados.get("Suficiente", 0)
            sinmin = conteo_estados.get("Sin mínimo", 0)
            # Mostrar métricas
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total registros", total_items)
            m2.metric("Críticos", criticos)
            m3.metric("Bajos", bajos)
            m4.metric("Suficientes", suficientes)
            # Tabla de productos en estado crítico o bajo
            df_alertas = stock_df[stock_df["Estado"].isin(["Crítico", "Bajo"])]
            if not df_alertas.empty:
                st.markdown("### 🛑 Productos con stock crítico o bajo")
                def color_alerta(row):
                    return [
                        "background-color: #ffcccc" if row["Estado"] == "Crítico" else "background-color: #fff2cc"
                    ] * len(row)
                st.dataframe(df_alertas.style.apply(color_alerta, axis=1), use_container_width=True)
            else:
                st.success("No hay productos en estado crítico ni bajo.")
            # Gráfico diario de entradas y salidas
            inventario_df["Fecha_dt"] = pd.to_datetime(inventario_df["Fecha"])
            inventario_df["Día"] = inventario_df["Fecha_dt"].dt.date
            df_diario = inventario_df.groupby(["Día", "Tipo"], as_index=False)["Cantidad"].sum()
            st.markdown("### 📅 Entradas y Salidas por Día")
            fig_diario = px.bar(
                df_diario,
                x="Día",
                y="Cantidad",
                color="Tipo",
                title="Entradas y salidas diarias",
                labels={"Cantidad": "Cantidad", "Día": "Fecha"},
                template="plotly_white",
            )
            fig_diario.update_layout(
                xaxis_title="Fecha",
                yaxis_title="Cantidad",
                legend_title="Tipo de movimiento",
                margin=dict(l=40, r=20, t=50, b=40),
            )
            st.plotly_chart(fig_diario, use_container_width=True)
            # Top productos por salidas
            df_salidas = inventario_df[inventario_df["Tipo"].str.contains("Salida")]
            if not df_salidas.empty:
                df_top = (
                    df_salidas.groupby(["Producto"], as_index=False)["Cantidad"]
                    .sum()
                    .sort_values(by="Cantidad")
                )
                df_top["Cantidad_abs"] = df_top["Cantidad"].abs()
                df_top = df_top.head(10)
                st.markdown("### 🏆 Top productos por salidas acumuladas")
                fig_top = px.bar(
                    df_top,
                    x="Producto",
                    y="Cantidad_abs",
                    title="Productos con mayores salidas (acumulado)",
                    labels={"Cantidad_abs": "Cantidad (valor absoluto)"},
                    template="plotly_white",
                )
                fig_top.update_layout(
                    xaxis_title="Producto",
                    yaxis_title="Cantidad (abs)",
                    margin=dict(l=40, r=20, t=50, b=40),
                )
                st.plotly_chart(fig_top, use_container_width=True)
            # Gráfico de stock por categoría
            if "Categoria" in catalogo_df.columns:
                df_cat = stock_df.merge(
                    catalogo_df[["Nombre", "Categoria"]].drop_duplicates(subset=["Nombre"]),
                    left_on="Producto",
                    right_on="Nombre",
                    how="left",
                )
                df_cat["Categoria"] = df_cat["Categoria"].fillna("Sin categoría")
                df_cat_group = df_cat.groupby("Categoria", as_index=False)["Stock"].sum()
                if not df_cat_group.empty:
                    st.markdown("### 📦 Stock teórico por categoría")
                    fig_cat = px.bar(
                        df_cat_group,
                        x="Categoria",
                        y="Stock",
                        title="Stock teórico por categoría",
                        labels={"Stock": "Stock teórico", "Categoria": "Categoría"},
                        template="plotly_white",
                    )
                    fig_cat.update_layout(
                        xaxis_title="Categoría",
                        yaxis_title="Stock",
                        margin=dict(l=40, r=20, t=50, b=40),
                    )
                    st.plotly_chart(fig_cat, use_container_width=True)


# ============================
# Módulo: Catálogo
# ============================
if "catalogo" in tab_dict:
    with tab_dict["catalogo"]:
        st.subheader("📘 Catálogo de Productos")
        df_catalogo = st.session_state["Catalogo"]
        # Formulario para añadir producto (solo admin o almacenista pueden agregar)
        if st.session_state.get("rol") in ["admin", "almacenista"]:
            # Se divide en tres columnas para capturar el nombre, tipo y categoría, y dos columnas
            # para la capacidad y el stock mínimo. La categoría permite agrupar los productos
            # por familias (por ejemplo, Ron, Vino, Cordiales).
            with st.form("form_catalogo"):
                col1, col2, col3 = st.columns(3)
                with col1:
                    nombre = st.text_input("Nombre del producto", value="")
                with col2:
                    tipo = st.selectbox("Tipo", ["Botella", "Trago", "Ingrediente"])
                with col3:
                    categoria = st.text_input("Categoría (familia)", value="")
                col4, col5 = st.columns(2)
                with col4:
                    capacidad_ml = st.number_input(
                        "Capacidad (ml)", min_value=0, step=50, value=0, help="Mililitros por unidad"
                    )
                with col5:
                    stock_minimo = st.number_input(
                        "Stock mínimo (opcional)", min_value=0, step=1, value=0
                    )
                submitted = st.form_submit_button("Agregar al Catálogo")
                if submitted:
                    if not nombre:
                        st.warning("Debes indicar el nombre del producto.")
                    else:
                        nuevo = pd.DataFrame([
                            {
                                "Nombre": nombre,
                                "Tipo": tipo,
                                "Categoria": categoria,
                                "ML": capacidad_ml,
                                "Stock Min": stock_minimo,
                            }
                        ])
                        st.session_state["Catalogo"] = pd.concat(
                            [df_catalogo, nuevo], ignore_index=True
                        )
                        exportar_a_google_sheets("Catalogo", st.session_state["Catalogo"])
                        st.success("Producto agregado al catálogo.")
        else:
            st.info("Solo el administrador o el almacenista pueden agregar productos al catálogo.")
        # Mostrar catálogo
        st.markdown("### 📋 Vista del Catálogo")
        st.dataframe(st.session_state["Catalogo"], use_container_width=True)


# ============================
# Módulo: Entradas
# ============================
if "entradas" in tab_dict:
    with tab_dict["entradas"]:
        st.subheader("📦 Registro de Entradas de Productos")
        # Comprobar rol permitido (almacenista o admin)
        if usuario_con_acceso(["almacenista", "admin"]):
            catalogo = st.session_state["Catalogo"]
            if catalogo.empty:
                st.warning("Catálogo vacío. Carga productos primero.")
            else:
                with st.form("form_entrada"):
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        producto = st.selectbox(
                            "Producto", catalogo["Nombre"].unique(), key="entrada_prod"
                        )
                    with col2:
                        cantidad = st.number_input(
                            "Cantidad (botellas/unidades)", min_value=0.1, value=1.0
                        )
                    with col3:
                        ubicacion = st.selectbox(
                            "Ubicación", UBICACIONES, index=0, key="entrada_ubic"
                        )
                    fecha = st.date_input(
                        "Fecha de ingreso", value=date.today(), key="entrada_fecha"
                    )
                    hora = st.time_input(
                        "Hora", value=datetime.now().time(), key="entrada_hora"
                    )
                    registrar = st.form_submit_button("Registrar Entrada")
                    if registrar:
                        # Preparar registro de entrada
                        dt = datetime.combine(fecha, hora)
                        registro = pd.DataFrame(
                            [
                                {
                                    "Fecha": dt,
                                    "Tipo": "Entrada",
                                    "Producto": producto,
                                    "Cantidad": cantidad,
                                    "Ubicación": ubicacion,
                                    "Usuario": usuario,
                                }
                            ]
                        )
                        # Actualizar inventario
                        actualizar_inventario_registro(st.session_state["Inventario"], registro)
                        # Actualizar hoja específica de entradas
                        df_entradas = st.session_state["Entradas"]
                        df_entradas = pd.concat([df_entradas, registro], ignore_index=True)
                        st.session_state["Entradas"] = df_entradas
                        exportar_a_google_sheets("Entradas", df_entradas)
                        st.success("Entrada registrada correctamente.")
                        st.rerun()
        else:
            st.info("No tienes permiso para registrar entradas.")


# ============================
# Módulo: Transferencias
# ============================
if "transferencias" in tab_dict:
    with tab_dict["transferencias"]:
        st.subheader("🔄 Transferencias de Producto")
        if usuario_con_acceso(["almacenista", "admin"]):
            catalogo = st.session_state["Catalogo"]
            if catalogo.empty:
                st.warning("Catálogo vacío. Carga productos primero.")
            else:
                with st.form("form_transferencia"):
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        producto = st.selectbox(
                            "Producto", catalogo["Nombre"].unique(), key="trans_prod"
                        )
                    with col2:
                        cantidad = st.number_input(
                            "Cantidad a transferir", min_value=0.1, value=1.0, step=0.1
                        )
                    with col3:
                        origen = st.selectbox(
                            "Origen", UBICACIONES, index=0, key="trans_origen"
                        )
                        destino = st.selectbox(
                            "Destino", UBICACIONES, index=1, key="trans_destino"
                        )
                    fecha = st.date_input(
                        "Fecha de transferencia", value=date.today(), key="trans_fecha"
                    )
                    hora = st.time_input(
                        "Hora", value=datetime.now().time(), key="trans_hora"
                    )
                    registrar = st.form_submit_button("Registrar Transferencia")
                    if registrar:
                        if origen == destino:
                            st.warning("El origen y el destino no pueden ser iguales.")
                        else:
                            dt = datetime.combine(fecha, hora)
                            # Registro negativo en origen
                            registro_origen = {
                                "Fecha": dt,
                                "Tipo": "Transferencia",
                                "Producto": producto,
                                "Cantidad": -cantidad,
                                "Ubicación": origen,
                                "Usuario": usuario,
                            }
                            # Registro positivo en destino
                            registro_destino = {
                                "Fecha": dt,
                                "Tipo": "Transferencia",
                                "Producto": producto,
                                "Cantidad": cantidad,
                                "Ubicación": destino,
                                "Usuario": usuario,
                            }
                            registros = pd.DataFrame([registro_origen, registro_destino])
                            # Actualizar inventario
                            actualizar_inventario_registro(st.session_state["Inventario"], registros)
                            # Actualizar hoja transferencias
                            df_transf = st.session_state["Transferencias"]
                            registro_transf = pd.DataFrame(
                                [
                                    {
                                        "Fecha": dt,
                                        "Producto": producto,
                                        "Cantidad": cantidad,
                                        "Origen": origen,
                                        "Destino": destino,
                                        "Usuario": usuario,
                                    }
                                ]
                            )
                            df_transf = pd.concat([
                                df_transf, registro_transf
                            ], ignore_index=True)
                            st.session_state["Transferencias"] = df_transf
                            exportar_a_google_sheets("Transferencias", df_transf)
                            st.success("Transferencia registrada correctamente.")
                            st.rerun()
        else:
            st.info("No tienes permiso para registrar transferencias.")


# ============================
# Módulo: Devoluciones
# ============================
if "devoluciones" in tab_dict:
    with tab_dict["devoluciones"]:
        st.subheader("♻️ Registro de Devoluciones")
        # Pueden registrar devoluciones bartender, almacenista o admin
        if usuario_con_acceso(["bartender", "almacenista", "admin"]):
            catalogo = st.session_state["Catalogo"]
            if catalogo.empty:
                st.warning("Catálogo vacío. Carga productos primero.")
            else:
                with st.form("form_devolucion"):
                    # Selección de producto y cantidades
                    col1, col2 = st.columns(2)
                    with col1:
                        producto = st.selectbox(
                            "Producto devuelto", catalogo["Nombre"].unique(), key="devol_prod"
                        )
                    with col2:
                        cantidad = st.number_input(
                            "Cantidad devuelta", min_value=0.1, value=1.0, step=0.1
                        )
                    # Selección de ubicaciones de origen y destino
                    col_loc1, col_loc2 = st.columns(2)
                    with col_loc1:
                        # Permitir origen externo para indicar que la devolución procede de un cliente
                        origen_options = UBICACIONES + ["Cliente/Externo"]
                        origen = st.selectbox(
                            "Origen de la devolución", origen_options,
                            index=len(origen_options) - 1, key="devol_origen"
                        )
                    with col_loc2:
                        destino = st.selectbox(
                            "Destino de la devolución", UBICACIONES, index=0, key="devol_destino"
                        )
                    # Fecha y hora
                    fecha = st.date_input(
                        "Fecha de devolución", value=date.today(), key="devol_fecha"
                    )
                    hora = st.time_input(
                        "Hora", value=datetime.now().time(), key="devol_hora"
                    )
                    motivo = st.text_input(
                        "Motivo de la devolución (opcional)", value="", key="devol_motivo"
                    )
                    registrar = st.form_submit_button("Registrar Devolución")
                    if registrar:
                        dt = datetime.combine(fecha, hora)
                        # Construir movimientos de inventario. Si el origen no es externo se
                        # descuenta la cantidad del origen, y siempre se suma en el destino.
                        movimientos = []
                        if origen != "Cliente/Externo":
                            movimientos.append(
                                {
                                    "Fecha": dt,
                                    "Tipo": "Devolución",
                                    "Producto": producto,
                                    "Cantidad": -cantidad,
                                    "Ubicación": origen,
                                    "Usuario": usuario,
                                }
                            )
                        movimientos.append(
                            {
                                "Fecha": dt,
                                "Tipo": "Devolución",
                                "Producto": producto,
                                "Cantidad": cantidad,
                                "Ubicación": destino,
                                "Usuario": usuario,
                            }
                        )
                        df_mov = pd.DataFrame(movimientos)
                        actualizar_inventario_registro(st.session_state["Inventario"], df_mov)
                        # Registrar en hoja Devoluciones (con Origen y Destino individuales)
                        registro_dev = pd.DataFrame(
                            [
                                {
                                    "Fecha": dt,
                                    "Producto": producto,
                                    "Cantidad": cantidad,
                                    "Origen": origen,
                                    "Destino": destino,
                                    "Usuario": usuario,
                                    "Motivo": motivo,
                                }
                            ]
                        )
                        df_dev = st.session_state["Devoluciones"]
                        df_dev = pd.concat([df_dev, registro_dev], ignore_index=True)
                        st.session_state["Devoluciones"] = df_dev
                        exportar_a_google_sheets("Devoluciones", df_dev)
                        st.success("Devolución registrada correctamente.")
                        st.rerun()
        else:
            st.info("No tienes permiso para registrar devoluciones.")


# ============================
# Módulo: Salidas (Botellas y Tragos)
# ============================
if "salidas" in tab_dict:
    with tab_dict["salidas"]:
        st.subheader("🚚 Registrar Salidas")
        # Obtener catálogo y recetas
        catalogo = st.session_state["Catalogo"]
        recetas_df = st.session_state["Recetas"]
        # Asegurar que salidas exista en el estado
        if "Salidas" not in st.session_state:
            st.session_state["Salidas"] = pd.DataFrame(columns=hojas_y_columnas["Salidas"])
        # Determinar si el usuario puede registrar salidas
        rol_actual = st.session_state.get("rol", "")
        puede_salidas = rol_actual in ["bartender", "almacenista", "admin"]
        if not puede_salidas:
            st.info("Este usuario solo tiene permisos de lectura en esta sección.")
        # Dividir el espacio en dos columnas para botellas y tragos
        col_botellas, col_tragos = st.columns(2)
        # ---- Formulario para Botellas ----
        with col_botellas:
            st.markdown("### Botellas")
            if catalogo.empty:
                st.warning("Catálogo vacío. Carga productos primero.")
            elif puede_salidas:
                with st.form("form_salida_botellas"):
                    producto = st.selectbox(
                        "Producto", catalogo["Nombre"].unique(), key="salida_botella_prod"
                    )
                    cantidad = st.number_input(
                        "Cantidad de botellas", min_value=0.1, step=0.1, value=1.0,
                        key="salida_botella_cant"
                    )
                    ubicacion = st.selectbox(
                        "Ubicación", UBICACIONES, key="salida_botella_ubic"
                    )
                    fecha_manual = st.date_input(
                        "Fecha", value=date.today(), key="salida_botella_fecha"
                    )
                    hora_manual = st.time_input(
                        "Hora", value=datetime.now().time(), key="salida_botella_hora"
                    )
                    registrar_bot = st.form_submit_button("Registrar Botella")
                    if registrar_bot:
                        dt = datetime.combine(fecha_manual, hora_manual)
                        # Registrar en inventario
                        registro_inv = pd.DataFrame(
                            [
                                {
                                    "Fecha": dt,
                                    "Tipo": "Salida Botella",
                                    "Producto": producto,
                                    "Cantidad": -cantidad,
                                    "Ubicación": ubicacion,
                                    "Usuario": usuario,
                                }
                            ]
                        )
                        actualizar_inventario_registro(st.session_state["Inventario"], registro_inv)
                        # Registrar en hoja de salidas
                        nueva_salida = {
                            "Fecha": dt,
                            "Producto/Trago": producto,
                            "Cantidad": -cantidad,
                            "Usuario": usuario,
                            "Ubicación": ubicacion,
                            "Tipo": "Salida Botella",
                        }
                        df_sal = st.session_state["Salidas"]
                        df_sal = pd.concat([df_sal, pd.DataFrame([nueva_salida])], ignore_index=True)
                        st.session_state["Salidas"] = df_sal
                        exportar_a_google_sheets("Salidas", df_sal)
                        st.success("✅ Salida de botella registrada.")
                        st.rerun()
        # ---- Formulario para Tragos ----
        with col_tragos:
            st.markdown("### Tragos")
            if recetas_df.empty:
                st.warning("No hay recetas registradas. Crea recetas en la pestaña correspondiente.")
            elif puede_salidas:
                with st.form("form_salida_tragos"):
                    trago = st.selectbox(
                        "Trago preparado", recetas_df["Trago"].unique(), key="salida_trago"
                    )
                    cantidad_tragos = st.number_input(
                        "Cantidad de tragos servidos", min_value=1, step=1, value=1,
                        key="salida_trago_cant"
                    )
                    ubicacion = st.selectbox(
                        "Ubicación", UBICACIONES, key="salida_trago_ubic"
                    )
                    fecha_manual = st.date_input(
                        "Fecha", value=date.today(), key="salida_trago_fecha"
                    )
                    hora_manual = st.time_input(
                        "Hora", value=datetime.now().time(), key="salida_trago_hora"
                    )
                    registrar_trago = st.form_submit_button("Registrar Trago")
                    if registrar_trago:
                        dt = datetime.combine(fecha_manual, hora_manual)
                        # Filtrar ingredientes del trago
                        ingredientes = recetas_df[recetas_df["Trago"] == trago]
                        salidas_registros = []
                        # Determinar el nombre de la columna que contiene el volumen
                        vol_col = "Cantidad_ml"
                        if "Cantidad_ml" not in ingredientes.columns and "ml" in ingredientes.columns:
                            vol_col = "ml"
                        for _, row in ingredientes.iterrows():
                            # Cantidad usada en litros (1L = 1000ml)
                            try:
                                ml_valor = float(row[vol_col])
                            except KeyError:
                                ml_cols = [c for c in row.index if c.lower() in ["cantidad_ml", "ml"]]
                                ml_valor = float(row[ml_cols[0]]) if ml_cols else 0
                            cantidad_litros = (ml_valor * cantidad_tragos) / 1000
                            salidas_registros.append(
                                {
                                    "Fecha": dt,
                                    "Tipo": "Salida Trago",
                                    "Producto": row["Ingrediente"],
                                    "Cantidad": -cantidad_litros,
                                    "Ubicación": ubicacion,
                                    "Usuario": usuario,
                                }
                            )
                        # Actualizar inventario con salidas de ingredientes
                        df_salidas = pd.DataFrame(salidas_registros)
                        actualizar_inventario_registro(st.session_state["Inventario"], df_salidas)
                        # Registrar resumen de trago
                        registro_trago = pd.DataFrame(
                            [
                                {
                                    "Fecha": dt,
                                    "Producto/Trago": trago,
                                    "Cantidad": -cantidad_tragos,
                                    "Usuario": usuario,
                                    "Ubicación": ubicacion,
                                    "Tipo": "Salida Trago",
                                }
                            ]
                        )
                        st.session_state["Salidas"] = pd.concat(
                            [st.session_state["Salidas"], registro_trago], ignore_index=True
                        )
                        exportar_a_google_sheets("Salidas", st.session_state["Salidas"])
                        # Registrar consumo de cada ingrediente en la hoja Consumos
                        df_consumos = st.session_state.get("Consumos", pd.DataFrame())
                        consumos_nuevos = []
                        for item in salidas_registros:
                            consumos_nuevos.append(
                                {
                                    "Fecha": item["Fecha"],
                                    "Trago": trago,
                                    "Ingrediente": item["Producto"],
                                    "Cantidad_Usada": -item["Cantidad"],
                                    "Ubicación": ubicacion,
                                    "Usuario": usuario,
                                }
                            )
                        df_consumos = pd.concat(
                            [df_consumos, pd.DataFrame(consumos_nuevos)], ignore_index=True
                        )
                        st.session_state["Consumos"] = df_consumos
                        exportar_a_google_sheets("Consumos", df_consumos)
                        st.success(
                            f"✅ {cantidad_tragos} trago(s) de {trago} registrado(s) y consumo de ingredientes guardado."
                        )
                        st.rerun()


# ============================
# Módulo: Stock por ubicación
# ============================
if "stock" in tab_dict:
    with tab_dict["stock"]:
        st.subheader("📦 Stock Actual por Ubicación")
        inventario_df = st.session_state["Inventario"]
        if inventario_df.empty:
            st.info("Aún no se han registrado movimientos de inventario.")
        else:
            stock_df = calcular_stock(inventario_df)
            # Permitir filtrar por ubicación
            ubic_seleccion = st.multiselect(
                "Filtrar por ubicación", UBICACIONES, default=UBICACIONES
            )
            if ubic_seleccion:
                stock_df = stock_df[stock_df["Ubicación"].isin(ubic_seleccion)]

            # Permitir filtrar por categoría si está disponible en el catálogo
            catalogo_df = st.session_state["Catalogo"]
            if "Categoria" in catalogo_df.columns:
                categorias_disponibles = [c for c in catalogo_df["Categoria"].dropna().unique() if c != ""]
                if categorias_disponibles:
                    cat_sel = st.multiselect(
                        "Filtrar por categoría", categorias_disponibles
                    )
                    if cat_sel:
                        prods_cat = catalogo_df[catalogo_df["Categoria"].isin(cat_sel)]["Nombre"]
                        stock_df = stock_df[stock_df["Producto"].isin(prods_cat)]

            # Calcular estado en función del stock mínimo (convertir a float para evitar errores)
            def calcular_estado(stock, minimo):
                if minimo == 0:
                    return "Sin mínimo"
                elif stock < minimo:
                    return "Crítico"
                elif stock < minimo * 2:
                    return "Bajo"
                else:
                    return "Suficiente"

            estados = []
            for _, row in stock_df.iterrows():
                prod = row["Producto"]
                min_vals = catalogo_df.loc[catalogo_df["Nombre"] == prod, "Stock Min"].values
                min_val = min_vals[0] if len(min_vals) > 0 else 0
                try:
                    min_val_float = float(min_val)
                except (ValueError, TypeError):
                    min_val_float = 0.0
                estados.append(calcular_estado(row["Stock"], min_val_float))
            stock_df = stock_df.copy()
            stock_df["Estado"] = estados
            # Asociar categoría a cada producto en el stock. Se utiliza un
            # mapeo con índice único para evitar errores cuando hay
            # productos duplicados en el catálogo.
            if "Categoria" in catalogo_df.columns:
                categoria_map = (
                    catalogo_df.drop_duplicates(subset=["Nombre"]).set_index("Nombre")["Categoria"]
                )
                stock_df["Categoria"] = stock_df["Producto"].map(categoria_map)
            # Estilo para resaltar estados
            def estilizar_fila(row):
                estado = row["Estado"]
                if estado == "Crítico":
                    color = "background-color: #ffcccc"
                elif estado == "Bajo":
                    color = "background-color: #fff2cc"
                elif estado == "Suficiente":
                    color = "background-color: #e6ffcc"
                else:
                    color = ""
                return [color] * len(row)

            st.markdown("### Tabla de Stock por Producto y Ubicación")
            styled = stock_df.style.apply(estilizar_fila, axis=1)
            st.dataframe(styled, use_container_width=True)
            # Gráfico de barras interactivo con diseño mejorado
            fig = px.bar(
                stock_df,
                x="Producto",
                y="Stock",
                color="Ubicación",
                barmode="group",
                title="Stock por Producto y Ubicación",
                labels={"Stock": "Cantidad", "Producto": "Producto"},
                template="plotly_white",
            )
            fig.update_layout(
                legend_title="Ubicación",
                xaxis_title="Producto",
                yaxis_title="Stock",
                margin=dict(l=40, r=20, t=50, b=40),
            )
            st.plotly_chart(fig, use_container_width=True)


# ============================
# Módulo: Auditoría Diaria
# ============================
if "auditoria_diaria" in tab_dict:
    with tab_dict["auditoria_diaria"]:
        # Título general de la sección
        st.subheader("📝 Auditoría Diaria de Stock")
        # Comprobar existencia de movimientos. Sin inventario no se puede auditar
        inventario_df = st.session_state["Inventario"]
        if inventario_df.empty:
            st.info("No hay movimientos registrados. No es posible auditar.")
        else:
            # Calcular el stock teórico actual en base al inventario
            stock_teorico = calcular_stock(inventario_df)
            if stock_teorico.empty:
                st.info("Inventario vacío. Nada que auditar.")
            else:
                # Determinar si el usuario puede registrar auditorías
                rol_actual = st.session_state.get("rol", "")
                puede_registrar = rol_actual in ["almacenista", "admin"]
                # Construir la lista de subtabs: si no se puede registrar, sólo la de consulta
                subtitulos = []
                if puede_registrar:
                    subtitulos.append("Registrar auditoría")
                subtitulos.append("Consultar auditorías")
                sub_tabs = st.tabs(subtitulos)
                # Mapeo para identificar cada subtabs
                idx = 0
                if puede_registrar:
                    # ========================
                    # Subtab: Registrar auditoría
                    # ========================
                    with sub_tabs[idx]:
                        st.markdown("#### Registrar conteo físico")
                        # Selección de fecha y turno (apertura/cierre)
                        colf, colt = st.columns([2, 1])
                        with colf:
                            fecha_audit = st.date_input(
                                "Fecha de auditoría", value=date.today(), key="fecha_auditaria2"
                            )
                        with colt:
                            turno = st.radio(
                                "Turno", ["Apertura", "Cierre"], index=0, horizontal=True, key="turno_audit2"
                            )
                        # Seleccionar ubicación a auditar. Puede ser una ubicación concreta o "Todas"
                        ubicacion_sel = st.selectbox(
                            "Ubicación", ["Todas"] + UBICACIONES, key="ubic_auditoria2"
                        )
                        # Filtrar el stock teórico por ubicación
                        if ubicacion_sel == "Todas":
                            df_teo = stock_teorico.copy()
                        else:
                            df_teo = stock_teorico[stock_teorico["Ubicación"] == ubicacion_sel].copy()
                        if df_teo.empty:
                            st.info("No hay stock en la ubicación seleccionada.")
                        else:
                            # Mostrar cada producto con su stock teórico y un campo para el stock físico.
                            valores_fisicos = {}
                        busqueda = st.text_input("Buscar producto", "")
                        if busqueda:
                            df_teo_iter = df_teo[df_teo["Producto"].str.contains(busqueda, case=False, na=False)].copy()
                        else:
                            df_teo_iter = df_teo.copy()
                            for i, fila in df_teo.iterrows():
                                prod = fila["Producto"]
                                ubic = fila["Ubicación"]
                                teorico = float(fila["Stock"])
                                colp, colt = st.columns([3, 1])
                                with colp:
                                    st.write(f"**{prod} ({ubic})** - Teórico: {teorico}")
                                with colt:
                                    valores_fisicos[f"fisico_{i}"] = st.number_input(
                                        "",
                                        value=0,
                                        step=1,
                                        format="%d",
                                        min_value=None,
                                        key=f"aud_fisico_{fecha_audit}_{turno}_{ubic}_{prod}_{i}"
                                    )
                            # Botón para guardar la auditoría
                            guardar_submit = st.button("Guardar auditoría", key=f"btn_guardar_aud_{fecha_audit}_{turno}")
                            if guardar_submit:
                                # Construir los datos a guardar
                                filas_guardar = []
                                for idx2, fila in df_teo.iterrows():
                                    teorico = float(fila["Stock"])
                                    fisico = st.session_state.get(
                                        f"aud_fisico_{fecha_audit}_{turno}_{fila['Ubicación']}_{fila['Producto']}_{idx2}",
                                        teorico,
                                    )
                                    filas_guardar.append(
                                        {
                                            "Fecha": fecha_audit,
                                            "Producto": fila["Producto"],
                                            "Ubicación": fila["Ubicación"],
                                            "Turno": turno,
                                            "Stock_Teorico": teorico,
                                            "Stock_Fisico": fisico,
                                            "Diferencia": fisico - teorico,
                                        }
                                    )
                                df_guardar = pd.DataFrame(filas_guardar)
                                # Actualizar StockFisico
                                df_stockfis = st.session_state["StockFisico"]
                                df_stockfis = pd.concat(
                                    [
                                        df_stockfis,
                                        df_guardar[["Fecha", "Producto", "Ubicación", "Turno", "Stock_Fisico"]],
                                    ],
                                    ignore_index=True,
                                )
                                st.session_state["StockFisico"] = df_stockfis
                                exportar_a_google_sheets("StockFisico", df_stockfis)
                                # Actualizar Auditoria_Diaria
                                df_aud_diaria = st.session_state["Auditoria_Diaria"]
                                df_aud_diaria = pd.concat([df_aud_diaria, df_guardar], ignore_index=True)
                                st.session_state["Auditoria_Diaria"] = df_aud_diaria
                                exportar_a_google_sheets("Auditoria_Diaria", df_aud_diaria)
                                # Mostrar resumen visual
                                st.success("Auditoría guardada correctamente. Resumen:")
                                resumen = df_guardar[["Producto", "Ubicación", "Stock_Teorico", "Stock_Fisico", "Diferencia"]].copy()
                                def colorear_dif(row):
                                    return ["background-color: #ffcccc" if row["Diferencia"] != 0 else ""] * len(row)
                                st.dataframe(resumen.style.apply(colorear_dif, axis=1), use_container_width=True)
                                fig_diff = px.bar(
                                    resumen,
                                    x="Producto",
                                    y="Diferencia",
                                    color="Ubicación",
                                    title="Diferencias de stock por producto",
                                    labels={"Diferencia": "Diferencia (Físico - Teórico)"},
                                    template="plotly_white",
                                )
                                fig_diff.update_layout(
                                    xaxis_title="Producto",
                                    yaxis_title="Diferencia",
                                    margin=dict(l=40, r=20, t=50, b=40),
                                )
                                st.plotly_chart(fig_diff, use_container_width=True)
                                # No se llama a st.rerun() aquí para permitir que el
                                # usuario vea el resumen de auditoría guardado. Los
                                # datos ya han sido exportados a Google Sheets y
                                # permanecerán en el estado actual. Si el usuario
                                # desea recargar los datos, puede utilizar el botón
                                # "Actualizar datos" en la barra lateral.
                    idx += 1
                # ========================
                # Subtab: Consultar auditorías
                # ========================
                with sub_tabs[idx]:
                    st.markdown("#### Consultar auditorías anteriores")
                    df_auditoria = st.session_state["Auditoria_Diaria"]
                    if df_auditoria.empty:
                        st.info("No hay auditorías registradas.")
                    else:
                        # Convertir la fecha a tipo date para filtrar
                        df_auditoria = df_auditoria.copy()
                        df_auditoria["Fecha_dt"] = pd.to_datetime(df_auditoria["Fecha"], errors="coerce").dt.date
                        fechas_unicas = sorted(df_auditoria["Fecha_dt"].dropna().unique(), reverse=True)
                        # Seleccionar fecha y turno
                        col_hist1, col_hist2, col_hist3 = st.columns([2, 1, 1])
                        with col_hist1:
                            fecha_hist = st.selectbox(
                                "Fecha", fechas_unicas, key="fecha_hist_consulta"
                            )
                        with col_hist2:
                            turno_hist = st.selectbox(
                                "Turno", ["Todos", "Apertura", "Cierre"], key="turno_hist_consulta"
                            )
                        with col_hist3:
                            ubic_hist = st.selectbox(
                                "Ubicación", ["Todas"] + UBICACIONES, key="ubic_hist_consulta"
                            )
                        # Filtrar registros según criterios
                        filtro = df_auditoria[df_auditoria["Fecha_dt"] == fecha_hist]
                        if turno_hist != "Todos":
                            filtro = filtro[filtro["Turno"] == turno_hist]
                        if ubic_hist != "Todas":
                            filtro = filtro[filtro["Ubicación"] == ubic_hist]
                        if filtro.empty:
                            st.info("No hay registros para los filtros seleccionados.")
                        else:
                            # Ordenar para una mejor visualización
                            filtro_orden = filtro.sort_values(by=["Producto", "Ubicación"])
                            # Mostrar tabla de auditoría
                            st.dataframe(
                                filtro_orden[
                                    ["Fecha", "Turno", "Producto", "Ubicación", "Stock_Teorico", "Stock_Fisico", "Diferencia"]
                                ],
                                use_container_width=True,
                            )
                            # Mostrar gráfico de diferencias por producto
                            fig_hist = px.bar(
                                filtro_orden,
                                x="Producto",
                                y="Diferencia",
                                color="Ubicación",
                                title="Diferencias por producto (auditoría seleccionada)",
                                labels={"Diferencia": "Diferencia"},
                                template="plotly_white",
                            )
                            fig_hist.update_layout(
                                xaxis_title="Producto",
                                yaxis_title="Diferencia",
                                margin=dict(l=40, r=20, t=50, b=40),
                            )
                            st.plotly_chart(fig_hist, use_container_width=True)


# ============================
# Módulo: Auditoría Semanal
# ============================
if "auditoria_semanal" in tab_dict:
    with tab_dict["auditoria_semanal"]:
        st.subheader("📊 Auditoría Semanal (Resumen)")
        # Solo roles almacenista y admin pueden generar nuevos reportes semanales.
        # Los supervisores pueden visualizar la información acumulada ya generada.
        rol_actual = st.session_state.get("rol", "")
        df_auditaria = st.session_state["Auditoria_Diaria"]
        if df_auditaria.empty:
            st.info("No hay auditorías diarias registradas aún.")
        else:
            hoy = date.today()
            inicio_semana = hoy - timedelta(days=hoy.weekday())
            fin_semana = inicio_semana - timedelta(days=1)
            inicio_semana_ant = fin_semana - timedelta(days=6)
            st.write(
                f"Semana analizada: {inicio_semana_ant} al {fin_semana} (semana anterior a la actual)."
            )
            # Filtrar auditorías del periodo anterior
            mask = (
                pd.to_datetime(df_auditaria["Fecha"]).dt.date >= inicio_semana_ant
            ) & (
                pd.to_datetime(df_auditaria["Fecha"]).dt.date <= fin_semana
            )
            semana_df = df_auditaria[mask]
            if semana_df.empty:
                st.info("No hay auditorías en la semana seleccionada.")
            else:
                # Calcular diferencia acumulada por producto y ubicación
                resumen = (
                    semana_df.groupby(["Producto", "Ubicación"], as_index=False)["Diferencia"]
                    .sum()
                    .rename(columns={"Diferencia": "Diferencia_Acumulada"})
                )
                st.markdown("### Diferencias acumuladas por producto y ubicación (semana anterior)")
                st.dataframe(resumen, use_container_width=True)
                # Guardar en hoja Auditoria_Semanal únicamente si el rol tiene permisos
                if rol_actual in ["almacenista", "admin"]:
                    df_aud_sem = st.session_state["Auditoria_Semanal"]
                    semana_id = inicio_semana_ant.strftime("%Y-%W")
                    resumen_reg = resumen.copy()
                    resumen_reg.insert(0, "Semana", semana_id)
                    df_aud_sem = pd.concat([df_aud_sem, resumen_reg], ignore_index=True)
                    st.session_state["Auditoria_Semanal"] = df_aud_sem
                    exportar_a_google_sheets("Auditoria_Semanal", df_aud_sem)
                # Gráfico de diferencias
                fig = px.bar(
                    resumen,
                    x="Producto",
                    y="Diferencia_Acumulada",
                    color="Ubicación",
                    title="Diferencia acumulada (Semana anterior)",
                    labels={"Diferencia_Acumulada": "Diferencia"},
                    template="plotly_white",
                )
                fig.update_layout(
                    xaxis_title="Producto",
                    yaxis_title="Diferencia",
                    margin=dict(l=40, r=20, t=50, b=40),
                )
                st.plotly_chart(fig, use_container_width=True)


# ============================
# Módulo: Historial de movimientos
# ============================
if "historial" in tab_dict:
    with tab_dict["historial"]:
        st.subheader("📜 Historial de Movimientos")
        inventario_df = st.session_state["Inventario"]
        if inventario_df.empty:
            st.info("No se han registrado movimientos en el inventario.")
        else:
            # Permitir filtros de fecha
            periodo = st.selectbox(
                "Rango de fechas", ["Hoy", "Última semana", "Último mes", "Todo", "Personalizado"]
            )
            if periodo == "Todo":
                inicio, fin = None, None
            elif periodo == "Personalizado":
                colf1, colf2 = st.columns(2)
                with colf1:
                    fecha_inicio = st.date_input("Fecha inicial", value=date.today() - timedelta(days=7))
                with colf2:
                    fecha_fin = st.date_input("Fecha final", value=date.today())
                inicio, fin = fecha_inicio, fecha_fin
            else:
                inicio, fin = obtener_intervalo_fechas(periodo)
            df_hist = inventario_df.copy()
            if inicio and fin:
                df_hist = df_hist[
                    (pd.to_datetime(df_hist["Fecha"]).dt.date >= inicio)
                    & (pd.to_datetime(df_hist["Fecha"]).dt.date <= fin)
                ]
            st.markdown("### Movimientos de Inventario")
            # Convertir la columna Fecha a datetime para ordenar de forma consistente. Los
            # valores que no puedan convertirse quedarán como NaT, lo que
            # permite que pandas los ordene sin errores.
            df_hist = df_hist.copy()
            df_hist["Fecha_dt"] = pd.to_datetime(df_hist["Fecha"], errors="coerce")
            df_hist_sorted = df_hist.sort_values(by=["Fecha_dt"], ascending=False)
            st.dataframe(df_hist_sorted.drop(columns=["Fecha_dt"]), use_container_width=True)
            # Opción de descarga
            csv = df_hist_sorted.drop(columns=["Fecha_dt"]).to_csv(index=False).encode("utf-8")
            st.download_button(
                "Descargar CSV", csv, file_name="historial_inventario.csv", mime="text/csv"
            )


# ============================
# Módulo: Recetas de Tragos
# ============================
if "recetas" in tab_dict:
    with tab_dict["recetas"]:
        st.subheader("📗 Recetas de Tragos")
        recetas_df = st.session_state["Recetas"]
        catalogo = st.session_state["Catalogo"]
        # Solo admin o almacenista pueden registrar nuevas recetas
        if st.session_state.get("rol") in ["admin", "almacenista"]:
            st.markdown("Agrega una receta compuesta por ingredientes del catálogo.")
            with st.form("form_receta"):
                col1, col2 = st.columns(2)
                with col1:
                    nombre_trago = st.text_input("Nombre del trago", key="nombre_trago")
                with col2:
                    cant_ingredientes = st.number_input(
                        "Cantidad de ingredientes", min_value=1, value=1, step=1, key="cant_ing"
                    )
                receta_data = []
                # Generar campos por ingrediente
                for i in range(int(cant_ingredientes)):
                    ing_col1, ing_col2 = st.columns([3, 1])
                    with ing_col1:
                        ingrediente = st.selectbox(
                            f"Ingrediente {i+1}", catalogo["Nombre"].unique(), key=f"ing_{i}"
                        )
                    with ing_col2:
                        ml = st.number_input(
                            f"ml {i+1}", min_value=1, value=30, key=f"ml_{i}"
                        )
                    receta_data.append({"Trago": nombre_trago, "Ingrediente": ingrediente, "Cantidad_ml": ml})
                registrar = st.form_submit_button("Registrar Receta")
                if registrar:
                    if not nombre_trago:
                        st.warning("Debes indicar el nombre del trago.")
                    else:
                        nueva_receta = pd.DataFrame(receta_data)
                        st.session_state["Recetas"] = pd.concat(
                            [recetas_df, nueva_receta], ignore_index=True
                        )
                        exportar_a_google_sheets("Recetas", st.session_state["Recetas"])
                        st.success("Receta registrada exitosamente.")
                        st.rerun()
        else:
            st.info("Solo el administrador o el almacenista pueden registrar nuevas recetas.")
        # Mostrar recetas existentes
        if not recetas_df.empty:
            st.markdown("### Recetas registradas")
            st.dataframe(recetas_df, use_container_width=True)



#
# Antiguo módulo de supervisión
#
# Se eliminó el bloque anterior que utilizaba "tabs" para renderizar
# un panel de supervisión adicional. Ahora todas las métricas
# correspondientes a supervisión y reportes están integradas en el
# módulo "Panel" y en la sección de "Stock". Si en un futuro se
# requiere un panel especializado, es preferible crearlo como un
# módulo independiente similar al "Panel" para mantener la coherencia
# con el esquema de pestañas dinámicas.
