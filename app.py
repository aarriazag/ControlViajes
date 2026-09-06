import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import psycopg2
import psycopg2.extras
import json
import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import closing

# Configuración de la página web con estilo e identidad corporativa
st.set_page_config(page_title="Ransa | Control de Ruta", layout="wide", page_icon="🚚")

# El servidor (Render) corre en otro huso horario — todos los usuarios de esta
# app están en Guatemala, así que fijamos la hora ahí en vez de usar la hora
# del servidor o intentar leer la del navegador de cada quien.
ZONA_HORARIA = ZoneInfo("America/Guatemala")


def ahora():
    return datetime.now(ZONA_HORARIA)

# --- SISTEMA DE DISEÑO RANSA ---
# Paleta: verde corporativo como color de marca, grises neutros para texto y
# fondos, tarjetas con sombra sutil y tipografía consistente — pensado para que
# la app se sienta como una herramienta interna de una empresa grande, no como
# un prototipo. Todo centralizado aquí para que un cambio de color se haga en
# un solo lugar.
st.markdown("""
    <style>
        :root {
            --ransa-verde: #0B4A32;
            --ransa-verde-oscuro: #072F20;
            --ransa-verde-claro: #E6EEEA;
            --ransa-naranja: #B5622E;
            --gris-texto: #1F2328;
            --gris-medio: #5B6169;
            --gris-borde: #DCDFE3;
            --gris-fondo: #F5F6F7;
            --gris-oscuro: #2E3338;
        }

        html, body, [class*="css"] {
            font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, Arial, sans-serif;
            color: var(--gris-texto);
        }

        /* Encabezados */
        h1, h2, h3 { letter-spacing: -0.01em; }
        h1 { font-weight: 700 !important; }
        h2, h3 { font-weight: 600 !important; }

        /* Botones primarios */
        div.stButton > button:first-child {
            background-color: var(--ransa-verde) !important;
            color: white !important;
            border-radius: 8px !important;
            border: none !important;
            font-weight: 600 !important;
            padding: 0.5rem 1.1rem !important;
            transition: background-color 0.15s ease-in-out;
            box-shadow: 0 1px 2px rgba(0,0,0,0.08);
        }
        div.stButton > button:first-child:hover {
            background-color: var(--ransa-verde-oscuro) !important;
            color: white !important;
        }
        div.stButton > button:disabled {
            background-color: var(--gris-borde) !important;
            color: var(--gris-medio) !important;
            box-shadow: none;
        }

        /* Pestañas */
        button[data-baseweb="tab"] {
            font-size: 15px !important;
            font-weight: 600 !important;
            color: var(--gris-medio);
        }
        button[aria-selected="true"] {
            color: var(--ransa-verde) !important;
            border-bottom: 3px solid var(--ransa-verde) !important;
        }
        div[data-baseweb="tab-highlight"] { background-color: var(--ransa-verde) !important; }

        /* Tarjetas (st.container(border=True)) con look "panel corporativo" */
        div[data-testid="stVerticalBlockBorderWrapper"] {
            border-radius: 12px !important;
            border: 1px solid var(--gris-borde) !important;
            box-shadow: 0 1px 3px rgba(16, 24, 40, 0.06);
            background-color: white;
        }

        /* Métricas tipo KPI card */
        div[data-testid="stMetric"] {
            background-color: white;
            border: 1px solid var(--gris-borde);
            border-radius: 10px;
            padding: 0.9rem 1rem;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
        }
        div[data-testid="stMetricLabel"] { color: var(--gris-medio) !important; font-weight: 600; }
        div[data-testid="stMetricValue"] { color: var(--ransa-verde) !important; }

        /* Sidebar */
        section[data-testid="stSidebar"] {
            background-color: var(--gris-fondo);
            border-right: 1px solid var(--gris-borde);
        }

        /* Alertas (success/info/warning/error) con bordes redondeados consistentes */
        div[data-testid="stAlert"] { border-radius: 8px; }

        /* Barra superior de marca */
        .ransa-topbar {
            display: flex; align-items: center; justify-content: space-between;
            padding: 14px 22px; border-radius: 12px;
            background: linear-gradient(90deg, var(--ransa-verde) 0%, var(--ransa-verde-oscuro) 100%);
            color: white; margin-bottom: 18px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.12);
        }
        .ransa-topbar .titulo { font-size: 20px; font-weight: 700; letter-spacing: -0.01em; }
        .ransa-topbar .subtitulo { font-size: 13px; opacity: 0.85; font-weight: 400; }
        .ransa-topbar .contexto {
            text-align: right; font-size: 13px; line-height: 1.5;
            background: rgba(255,255,255,0.12); padding: 6px 14px; border-radius: 8px;
        }
        .ransa-badge {
            display: inline-block; background: var(--ransa-verde-claro); color: var(--ransa-verde-oscuro);
            font-size: 11px; font-weight: 700; padding: 2px 9px; border-radius: 999px;
            letter-spacing: 0.03em; text-transform: uppercase; margin-left: 8px;
        }

        /* Reduce el padding superior por defecto de Streamlit para que la topbar quede pegada arriba */
        .block-container { padding-top: 1.6rem; }

        /* --- Inputs, selects, textareas: look de producto moderno, no de formulario
           de los 2000s --- */
        div[data-testid="stTextInput"] input,
        div[data-testid="stNumberInput"] input,
        div[data-testid="stTextArea"] textarea,
        div[data-baseweb="select"] > div {
            border-radius: 8px !important;
            border-color: var(--gris-borde) !important;
            font-size: 14px !important;
        }
        div[data-testid="stTextInput"] input:focus,
        div[data-testid="stNumberInput"] input:focus,
        div[data-testid="stTextArea"] textarea:focus,
        div[data-baseweb="select"] > div:focus-within {
            border-color: var(--ransa-verde) !important;
            box-shadow: 0 0 0 2px rgba(0, 105, 62, 0.15) !important;
        }
        label[data-testid="stWidgetLabel"] p {
            font-size: 13px !important;
            font-weight: 600 !important;
            color: var(--gris-texto) !important;
        }

        /* Toggle en verde de marca en vez del rojo/azul por defecto */
        div[data-testid="stToggle"] label div[data-checked="true"] {
            background-color: var(--ransa-verde) !important;
        }

        /* Expanders con el mismo look de tarjeta que el resto de la app */
        div[data-testid="stExpander"] {
            border: 1px solid var(--gris-borde) !important;
            border-radius: 10px !important;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
        }

        /* Alertas con acento de color a la izquierda, look más "SaaS" */
        div[data-testid="stAlertContentSuccess"] { border-left: 4px solid #16794C; padding-left: 10px; }
        div[data-testid="stAlertContentInfo"] { border-left: 4px solid #2563AE; padding-left: 10px; }
        div[data-testid="stAlertContentWarning"] { border-left: 4px solid #B7791F; padding-left: 10px; }
        div[data-testid="stAlertContentError"] { border-left: 4px solid #C53030; padding-left: 10px; }

        /* Encabezados de sección con un poco más de aire */
        h3, h4 { margin-top: 0.6rem; }

        /* Dataframes con esquinas redondeadas consistentes */
        div[data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; border: 1px solid var(--gris-borde); }

        /* Círculo numerado para cada destino, estilo "paso a paso" */
        .badge-numero {
            width: 28px; height: 28px; border-radius: 50%;
            background: var(--ransa-verde); color: white;
            display: flex; align-items: center; justify-content: center;
            font-weight: 700; font-size: 13px; margin-top: 6px;
        }

        /* Botón primario (type="primary") con look de CTA oscuro, como el mockup */
        div.stButton > button[kind="primary"] {
            background-color: var(--gris-oscuro) !important;
            font-size: 15px !important;
            padding: 0.7rem 1.1rem !important;
        }
        div.stButton > button[kind="primary"]:hover {
            background-color: #14181c !important;
        }
        </style>
""", unsafe_allow_html=True)

# ==========================================
# CAPA DE BASE DE DATOS (Postgres / Supabase)
# Reemplaza session_state y SQLite: los datos viven en Supabase, así que
# sobreviven a reinicios del servidor y se comparten entre todos los usuarios
# (despachador y liquidador ven la misma información en tiempo real).
#
# Config necesaria en .streamlit/secrets.toml (nunca subir este archivo a GitHub):
#   [postgres]
#   host = "db.xxxxxxxxxxxx.supabase.co"
#   port = 5432
#   dbname = "postgres"
#   user = "postgres"
#   password = "..."
# ==========================================

def get_conn():
    cfg = st.secrets["postgres"]
    return psycopg2.connect(
        host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
        user=cfg["user"], password=cfg["password"]
    )


def init_db():
    """Crea las tablas si no existen todavía. Se puede correr las veces que sea:
    no borra ni duplica nada si las tablas ya están creadas."""
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contadores (
                cliente TEXT PRIMARY KEY,
                ultimo_correlativo INTEGER NOT NULL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS viajes (
                id SERIAL PRIMARY KEY,
                id_viaje TEXT UNIQUE NOT NULL,
                cliente TEXT NOT NULL,
                placa TEXT NOT NULL,
                transportista TEXT,
                piloto TEXT,
                auxiliar TEXT,
                usuario_creador TEXT,
                fecha_creacion TEXT,
                hora_creacion TEXT,
                estado TEXT DEFAULT 'Pendiente de Liquidar'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS destinos (
                id SERIAL PRIMARY KEY,
                viaje_id INTEGER NOT NULL REFERENCES viajes(id),
                orden INTEGER,
                tienda TEXT,
                km REAL,
                galones_base REAL,
                pedidos TEXT,
                marchamo_ida TEXT UNIQUE NOT NULL,
                marchamo_regreso TEXT,
                roles INTEGER DEFAULT 0,
                tarimas INTEGER DEFAULT 0,
                cajas INTEGER DEFAULT 0
            )
        """)
        # Columnas de liquidación (se agregan solas si no existen; no afecta datos ya guardados)
        cur.execute("ALTER TABLE destinos ADD COLUMN IF NOT EXISTS roles_devueltos INTEGER")
        cur.execute("ALTER TABLE destinos ADD COLUMN IF NOT EXISTS tarimas_devueltas INTEGER")
        cur.execute("ALTER TABLE destinos ADD COLUMN IF NOT EXISTS pacas_carton_devueltas INTEGER")
        cur.execute("ALTER TABLE viajes ADD COLUMN IF NOT EXISTS usuario_liquido TEXT")
        cur.execute("ALTER TABLE viajes ADD COLUMN IF NOT EXISTS fecha_liquidacion TEXT")
        cur.execute("ALTER TABLE viajes ADD COLUMN IF NOT EXISTS hora_liquidacion TEXT")
        cur.execute("ALTER TABLE viajes ADD COLUMN IF NOT EXISTS usuario_anulo TEXT")
        cur.execute("ALTER TABLE viajes ADD COLUMN IF NOT EXISTS fecha_anulacion TEXT")
        cur.execute("ALTER TABLE viajes ADD COLUMN IF NOT EXISTS motivo_anulacion TEXT")
        # Columnas para la Hoja de Control de Viaje (impresión)
        cur.execute("ALTER TABLE viajes ADD COLUMN IF NOT EXISTS cliente_principal TEXT")
        cur.execute("ALTER TABLE viajes ADD COLUMN IF NOT EXISTS cd_origen TEXT")
        cur.execute("ALTER TABLE destinos ADD COLUMN IF NOT EXISTS remitos TEXT")
        cur.execute("ALTER TABLE destinos ADD COLUMN IF NOT EXISTS incidencias TEXT")
        cur.execute("ALTER TABLE destinos ADD COLUMN IF NOT EXISTS devolucion TEXT")
        cur.execute("ALTER TABLE destinos ADD COLUMN IF NOT EXISTS creditos TEXT")
        cur.execute("ALTER TABLE destinos ADD COLUMN IF NOT EXISTS pg_cajas INTEGER")
        cur.execute("ALTER TABLE destinos ADD COLUMN IF NOT EXISTS es_complemento BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE destinos ADD COLUMN IF NOT EXISTS tipo_pago TEXT")

        # ---- Tablas de catálogos (antes vivían "quemadas" en el código Python) ----
        cur.execute("CREATE TABLE IF NOT EXISTS cat_transportistas (nombre TEXT PRIMARY KEY)")
        cur.execute("ALTER TABLE cat_transportistas ADD COLUMN IF NOT EXISTS razon_social TEXT")
        cur.execute("ALTER TABLE cat_transportistas ADD COLUMN IF NOT EXISTS codigo SERIAL")
        cur.execute("CREATE TABLE IF NOT EXISTS cat_pilotos (nombre TEXT PRIMARY KEY)")
        cur.execute("ALTER TABLE cat_pilotos ADD COLUMN IF NOT EXISTS codigo SERIAL")
        cur.execute("CREATE TABLE IF NOT EXISTS cat_auxiliares (nombre TEXT PRIMARY KEY)")
        cur.execute("ALTER TABLE cat_auxiliares ADD COLUMN IF NOT EXISTS codigo SERIAL")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cat_camiones (
                placa TEXT PRIMARY KEY, tipo TEXT, transportista TEXT, piloto TEXT, auxiliar TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cat_clientes_tiendas (
                cliente TEXT, tienda TEXT, km REAL,
                PRIMARY KEY (cliente, tienda)
            )
        """)
        # Código numérico de tienda: se digita a mano (debe combinar con el código
        # real que uses en otros sistemas — WMS, ERP, etc.) para poder cruzar
        # información más adelante.
        cur.execute("ALTER TABLE cat_clientes_tiendas ADD COLUMN IF NOT EXISTS codigo_tienda INTEGER")
        # El diésel ya NO se calcula con un galonaje fijo por tienda: depende del
        # rendimiento (km por galón) de cada tipo de camión, así que un viaje en una
        # unidad de 5 Ton consume distinto que uno de 10 Ton a la misma distancia.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cat_rendimiento_camion (
                tipo TEXT PRIMARY KEY, km_por_galon REAL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cat_cds_por_cliente (
                cliente TEXT, cd TEXT, PRIMARY KEY (cliente, cd)
            )
        """)
        cur.execute("CREATE TABLE IF NOT EXISTS cat_usuarios (usuario TEXT PRIMARY KEY, perfil TEXT)")
        # Qué clientes puede ver/trabajar cada usuario (excepto Administrador, que ve todos).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cat_usuario_clientes (
                usuario TEXT, cliente TEXT, PRIMARY KEY (usuario, cliente)
            )
        """)
        # Clientes como catálogo propio (antes solo existían "implícitos" dentro de
        # Clientes y Tiendas) — necesario para poder referenciarlos con llave foránea.
        cur.execute("CREATE TABLE IF NOT EXISTS cat_clientes (id SERIAL, nombre TEXT PRIMARY KEY)")
        conn.commit()

        # Sembrar datos de ejemplo SOLO la primera vez (tablas vacías), para que la
        # app no quede sin catálogos apenas se activa esta versión.
        cur.execute("SELECT COUNT(*) FROM cat_clientes_tiendas")
        if cur.fetchone()[0] == 0:
            cur.executemany("INSERT INTO cat_transportistas (nombre) VALUES (%s)",
                             [("Transportes Express",), ("Logística del Norte",), ("Flota Interna",)])
            cur.executemany("INSERT INTO cat_pilotos (nombre) VALUES (%s)",
                             [("Juan Pérez",), ("María Rodríguez",), ("Luis Martínez",), ("Andrés Custodio",)])
            cur.executemany("INSERT INTO cat_auxiliares (nombre) VALUES (%s)",
                             [("Carlos López",), ("Pedro Gómez",), ("José Hernández",), ("Ramiro Ruiz",)])
            cur.executemany(
                "INSERT INTO cat_camiones (placa, tipo, transportista, piloto, auxiliar) VALUES (%s,%s,%s,%s,%s)",
                [("C-123ABC", "5 Ton", "Transportes Express", "Juan Pérez", "Carlos López"),
                 ("C-456DEF", "10 Ton", "Logística del Norte", "María Rodríguez", "Pedro Gómez"),
                 ("C-789GHI", "20 Ton", "Flota Interna", "Luis Martínez", "José Hernández")]
            )
            cur.executemany(
                "INSERT INTO cat_clientes (nombre) VALUES (%s)",
                [("Dollarcity",), ("UniSuper",), ("UniSuper Importados",), ("UniSuper LTX",)]
            )
            cur.executemany(
                "INSERT INTO cat_clientes_tiendas (cliente, tienda, km) VALUES (%s,%s,%s)",
                [("Dollarcity", "Dollarcity Zona 10", 15.5),
                 ("Dollarcity", "Dollarcity Mixco", 32.0),
                 ("UniSuper", "UniSuper Central", 22.1),
                 ("UniSuper Importados", "UniSuper Importados Norte", 18.0),
                 ("UniSuper LTX", "UniSuper LTX Sur", 45.3)]
            )
            cur.executemany(
                "INSERT INTO cat_rendimiento_camion (tipo, km_por_galon) VALUES (%s,%s)",
                [("5 Ton", 8.0), ("10 Ton", 6.0), ("20 Ton", 4.0)]
            )
            cur.executemany(
                "INSERT INTO cat_cds_por_cliente (cliente, cd) VALUES (%s,%s)",
                [("Dollarcity", "CD Barcenas"), ("Dollarcity", "CD Central"),
                 ("UniSuper", "CD Barcenas"),
                 ("UniSuper Importados", "CD Barcenas"),
                 ("UniSuper LTX", "CD Barcenas")]
            )
            cur.executemany(
                "INSERT INTO cat_usuarios (usuario, perfil) VALUES (%s,%s)",
                [("Admin_Logistica", "Administrador"), ("Op_Salidas", "Operador"), ("Liq_Transporte", "Liquidador")]
            )
            # Por defecto, los usuarios de ejemplo (no-Administrador) ven todos los
            # clientes sembrados, para no romper nada mientras ajustas los accesos reales.
            cur.executemany(
                "INSERT INTO cat_usuario_clientes (usuario, cliente) VALUES (%s,%s)",
                [(u, c) for u in ("Op_Salidas", "Liq_Transporte")
                 for c in ("Dollarcity", "UniSuper", "UniSuper Importados", "UniSuper LTX")]
            )
            conn.commit()

        # Por si "cat_clientes" se creó después de ya tener datos (upgrade de una
        # versión anterior de la app): rellena con cualquier cliente que ya exista
        # disperso en otras tablas, para que las llaves foráneas de abajo no fallen.
        cur.execute("""
            INSERT INTO cat_clientes (nombre)
            SELECT DISTINCT cliente FROM (
                SELECT cliente FROM cat_clientes_tiendas
                UNION SELECT cliente FROM cat_cds_por_cliente
                UNION SELECT cliente FROM cat_usuario_clientes
                UNION SELECT cliente FROM viajes
            ) t
            ON CONFLICT (nombre) DO NOTHING
        """)
        conn.commit()

        # ---- Llaves foráneas con actualización en cascada ----
        # Esto es lo que de verdad evita que renombrar un piloto/auxiliar/
        # transportista/cliente en su catálogo rompa los viajes ya guardados: en
        # vez de que la app tenga que "saber" actualizar cada tabla relacionada,
        # Postgres lo hace solo. Se usa NOT VALID para no fallar si algún dato de
        # prueba ya guardado no calza exactamente (no se revisa lo viejo, solo se
        # exige hacia adelante).
        foreign_keys = [
            ("cat_camiones", "fk_camion_piloto", "piloto", "cat_pilotos", "nombre"),
            ("cat_camiones", "fk_camion_auxiliar", "auxiliar", "cat_auxiliares", "nombre"),
            ("cat_camiones", "fk_camion_transportista", "transportista", "cat_transportistas", "nombre"),
            ("cat_clientes_tiendas", "fk_tienda_cliente", "cliente", "cat_clientes", "nombre"),
            ("cat_cds_por_cliente", "fk_cd_cliente", "cliente", "cat_clientes", "nombre"),
            ("cat_usuario_clientes", "fk_uc_usuario", "usuario", "cat_usuarios", "usuario"),
            ("cat_usuario_clientes", "fk_uc_cliente", "cliente", "cat_clientes", "nombre"),
            ("viajes", "fk_viaje_cliente", "cliente", "cat_clientes", "nombre"),
            ("viajes", "fk_viaje_placa", "placa", "cat_camiones", "placa"),
            ("viajes", "fk_viaje_piloto", "piloto", "cat_pilotos", "nombre"),
            ("viajes", "fk_viaje_auxiliar", "auxiliar", "cat_auxiliares", "nombre"),
            ("viajes", "fk_viaje_transportista", "transportista", "cat_transportistas", "nombre"),
        ]
        for tabla, nombre_fk, columna, tabla_ref, columna_ref in foreign_keys:
            cur.execute(f"""
                DO $$
                BEGIN
                    ALTER TABLE {tabla} ADD CONSTRAINT {nombre_fk}
                        FOREIGN KEY ({columna}) REFERENCES {tabla_ref}({columna_ref})
                        ON UPDATE CASCADE ON DELETE RESTRICT NOT VALID;
                EXCEPTION WHEN duplicate_object THEN NULL;
                END $$;
            """)
        conn.commit()


def cargar_catalogos_desde_db():
    """Lee las 6 tablas de catálogos y arma el mismo diccionario que antes vivía
    quemado en el código, para que el resto de la app no tenga que cambiar nada."""
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT usuario, perfil FROM cat_usuarios")
        usuarios = {r["usuario"]: r["perfil"] for r in cur.fetchall()}

        cur.execute("SELECT nombre FROM cat_transportistas ORDER BY nombre")
        transportistas = [r["nombre"] for r in cur.fetchall()]

        cur.execute("SELECT nombre FROM cat_pilotos ORDER BY nombre")
        pilotos = [r["nombre"] for r in cur.fetchall()]

        cur.execute("SELECT nombre FROM cat_auxiliares ORDER BY nombre")
        auxiliares = [r["nombre"] for r in cur.fetchall()]

        cur.execute("SELECT placa, tipo, transportista, piloto, auxiliar FROM cat_camiones ORDER BY placa")
        camiones = {r["placa"]: {"tipo": r["tipo"], "transportista": r["transportista"],
                                  "piloto": r["piloto"], "auxiliar": r["auxiliar"]} for r in cur.fetchall()}

        cur.execute("SELECT cliente, tienda, km FROM cat_clientes_tiendas ORDER BY cliente, tienda")
        clientes = {}
        for r in cur.fetchall():
            clientes.setdefault(r["cliente"], {})[r["tienda"]] = {"km": r["km"]}

        cur.execute("SELECT tipo, km_por_galon FROM cat_rendimiento_camion")
        rendimiento = {r["tipo"]: r["km_por_galon"] for r in cur.fetchall()}

        cur.execute("SELECT cliente, cd FROM cat_cds_por_cliente ORDER BY cliente, cd")
        cds_por_cliente = {}
        for r in cur.fetchall():
            cds_por_cliente.setdefault(r["cliente"], []).append(r["cd"])

        cur.execute("SELECT usuario, cliente FROM cat_usuario_clientes")
        usuario_clientes = {}
        for r in cur.fetchall():
            usuario_clientes.setdefault(r["usuario"], []).append(r["cliente"])

        cur.execute("SELECT nombre FROM cat_clientes ORDER BY nombre")
        clientes_lista = [r["nombre"] for r in cur.fetchall()]

        return {
            "usuarios": usuarios,
            "transportistas": transportistas,
            "pilotos": pilotos,
            "auxiliares": auxiliares,
            "camiones": camiones,
            "clientes": clientes,
            "clientes_lista": clientes_lista,
            "rendimiento": rendimiento,
            "cds_por_cliente": cds_por_cliente,
            "usuario_clientes": usuario_clientes
        }


def clientes_permitidos_para(usuario, perfil):
    """Administrador ve todos los clientes; cualquier otro perfil solo ve los
    clientes que tenga asignados en el catálogo de accesos."""
    todos = list(st.session_state.catalogos["clientes"].keys())
    if perfil == "Administrador":
        return todos
    return [c for c in st.session_state.catalogos["usuario_clientes"].get(usuario, []) if c in todos]


def actualizar_default_camion(placa, piloto, auxiliar):
    """Cada vez que se guarda un viaje, el camión 'aprende' el piloto/auxiliar
    usado esta vez y lo deja como default para la próxima — así no siempre hay
    que asignarlo a mano, pero se puede seguir cambiando cuando haga falta."""
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE cat_camiones SET piloto=%s, auxiliar=%s WHERE placa=%s",
                    (piloto, auxiliar, placa)
                )
            conn.commit()
        except Exception:
            conn.rollback()  # si falla, no es crítico — el viaje ya se guardó bien


# Config genérica usada por la pantalla de Catálogos: qué tabla, columnas y
# llave primaria corresponden a cada catálogo, para no repetir código por cada uno.
CATALOGOS_CONFIG = {
    "Clientes": {"tabla": "cat_clientes", "columnas": ["nombre"], "clave": ["nombre"], "numericas": [],
                "solo_lectura": ["id"]},
    "Transportistas": {"tabla": "cat_transportistas", "columnas": ["nombre", "razon_social"], "clave": ["nombre"],
                       "numericas": [], "solo_lectura": ["codigo"]},
    "Pilotos": {"tabla": "cat_pilotos", "columnas": ["nombre"], "clave": ["nombre"], "numericas": [],
               "solo_lectura": ["codigo"]},
    "Auxiliares": {"tabla": "cat_auxiliares", "columnas": ["nombre"], "clave": ["nombre"], "numericas": [],
                  "solo_lectura": ["codigo"]},
    "Camiones": {"tabla": "cat_camiones", "columnas": ["placa", "tipo", "transportista", "piloto", "auxiliar"],
                 "clave": ["placa"], "numericas": []},
    "Clientes y Tiendas": {"tabla": "cat_clientes_tiendas", "columnas": ["cliente", "tienda", "codigo_tienda", "km"],
                           "clave": ["cliente", "tienda"], "numericas": ["codigo_tienda", "km"]},
    "Rendimiento por Camión": {"tabla": "cat_rendimiento_camion", "columnas": ["tipo", "km_por_galon"],
                               "clave": ["tipo"], "numericas": ["km_por_galon"]},
    "CDs por Cliente": {"tabla": "cat_cds_por_cliente", "columnas": ["cliente", "cd"],
                        "clave": ["cliente", "cd"], "numericas": []},
    "Usuarios": {"tabla": "cat_usuarios", "columnas": ["usuario", "perfil"], "clave": ["usuario"], "numericas": []},
    "Acceso Usuario → Cliente": {"tabla": "cat_usuario_clientes", "columnas": ["usuario", "cliente"],
                                 "clave": ["usuario", "cliente"], "numericas": []},
}


def agregar_o_actualizar_registro(tabla, columnas, clave, valores):
    """Inserta un registro nuevo, o lo actualiza si la llave ya existe (upsert),
    para poder corregir un solo dato sin tener que resubir todo el Excel."""
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                cols_sql = ", ".join(columnas)
                placeholders = ", ".join(["%s"] * len(columnas))
                no_clave = [c for c in columnas if c not in clave]
                set_sql = ", ".join(f"{c} = EXCLUDED.{c}" for c in no_clave)
                query = f"INSERT INTO {tabla} ({cols_sql}) VALUES ({placeholders})"
                if set_sql:
                    query += f" ON CONFLICT ({', '.join(clave)}) DO UPDATE SET {set_sql}"
                else:
                    query += f" ON CONFLICT ({', '.join(clave)}) DO NOTHING"
                cur.execute(query, tuple(valores[c] for c in columnas))
            conn.commit()
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, str(e)


def eliminar_registro(tabla, clave, valores_clave):
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                where_sql = " AND ".join(f"{c} = %s" for c in clave)
                cur.execute(f"DELETE FROM {tabla} WHERE {where_sql}", tuple(valores_clave[c] for c in clave))
            conn.commit()
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, str(e)


def generar_plantilla_excel(columnas):
    """Genera un Excel vacío (solo encabezados) con las columnas correctas,
    para que el usuario lo llene y lo vuelva a subir."""
    buffer = io.BytesIO()
    pd.DataFrame(columns=columnas).to_excel(buffer, index=False, engine="openpyxl")
    return buffer.getvalue()


def leer_catalogo_actual(tabla, columnas):
    with closing(get_conn()) as conn:
        return pd.read_sql_query(f"SELECT {', '.join(columnas)} FROM {tabla}", conn)


def sincronizar_catalogo(tabla, columnas, clave, df_nuevo):
    """Sincroniza un catálogo contra un DataFrame (Excel subido o editado en pantalla):
    agrega registros nuevos, actualiza los que cambiaron (upsert), y borra los que ya
    no aparecen — EXCEPTO si están en uso en otra tabla (Camiones, Viajes), en cuyo
    caso Postgres bloquea ESE borrado puntual (llave foránea) y seguimos con el resto,
    avisando al final cuáles no se pudieron quitar."""
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                # Si esta tabla tiene columna "cliente", hay que asegurar que esos
                # clientes ya existan en cat_clientes ANTES de sincronizar — si no,
                # la llave foránea rechaza cualquier cliente nuevo que venga en el archivo.
                if "cliente" in columnas and tabla != "cat_clientes":
                    clientes_del_archivo = {str(row["cliente"]).strip() for _, row in df_nuevo.iterrows() if str(row["cliente"]).strip()}
                    for c in clientes_del_archivo:
                        cur.execute("INSERT INTO cat_clientes (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING", (c,))

                cur.execute(f"SELECT {', '.join(clave)} FROM {tabla}")
                claves_actuales = {tuple(str(v) for v in row) for row in cur.fetchall()}
                claves_nuevas = {tuple(str(row[c]).strip() for c in clave) for _, row in df_nuevo.iterrows()}

                no_borrables = []
                for fila_clave in claves_actuales - claves_nuevas:
                    cur.execute("SAVEPOINT sp_borrado")
                    try:
                        where_sql = " AND ".join(f"{c} = %s" for c in clave)
                        cur.execute(f"DELETE FROM {tabla} WHERE {where_sql}", fila_clave)
                        cur.execute("RELEASE SAVEPOINT sp_borrado")
                    except Exception:
                        cur.execute("ROLLBACK TO SAVEPOINT sp_borrado")
                        no_borrables.append(" / ".join(fila_clave))

                cols_sql = ", ".join(columnas)
                placeholders = ", ".join(["%s"] * len(columnas))
                no_clave = [c for c in columnas if c not in clave]
                set_sql = ", ".join(f"{c} = EXCLUDED.{c}" for c in no_clave)
                query = f"INSERT INTO {tabla} ({cols_sql}) VALUES ({placeholders})"
                query += f" ON CONFLICT ({', '.join(clave)}) DO UPDATE SET {set_sql}" if set_sql \
                    else f" ON CONFLICT ({', '.join(clave)}) DO NOTHING"
                for _, row in df_nuevo.iterrows():
                    cur.execute(query, tuple(row[c] for c in columnas))

            conn.commit()
            if no_borrables:
                return True, f"⚠️ Guardado, pero esto sigue existiendo porque está en uso en Camiones o Viajes: {', '.join(no_borrables)}"
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, str(e)


init_db()


def marchamo_ya_usado(marchamo, cur):
    cur.execute("SELECT 1 FROM destinos WHERE marchamo_ida = %s", (marchamo,))
    return cur.fetchone() is not None


def peek_siguiente_correlativo(cliente):
    """Solo para mostrar una vista previa en pantalla; NO reserva el número."""
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT ultimo_correlativo FROM contadores WHERE cliente = %s", (cliente,))
        row = cur.fetchone()
        numero = (row[0] if row else 0) + 1
    prefijo = "".join([w[0] for w in cliente.split()]).upper()[:4]
    return f"{prefijo}-{numero:04d}"


def siguiente_correlativo(cliente, cur):
    """Incrementa de forma atómica el contador del cliente y devuelve el No. de Viaje.
    Al estar dentro de la misma transacción que el INSERT del viaje, Postgres bloquea
    la fila del contador hasta que se confirme, así que dos digitadores guardando al
    mismo tiempo NUNCA reciben el mismo número."""
    cur.execute(
        "INSERT INTO contadores (cliente, ultimo_correlativo) VALUES (%s, 0) "
        "ON CONFLICT (cliente) DO NOTHING", (cliente,)
    )
    cur.execute(
        "UPDATE contadores SET ultimo_correlativo = ultimo_correlativo + 1 "
        "WHERE cliente = %s RETURNING ultimo_correlativo", (cliente,)
    )
    numero = cur.fetchone()[0]
    prefijo = "".join([w[0] for w in cliente.split()]).upper()[:4]
    return f"{prefijo}-{numero:04d}"


def guardar_viaje(cliente, placa, transportista, piloto, auxiliar, usuario, destinos_viaje, cd_origen=None):
    """Guarda el viaje y sus destinos en una sola transacción.
    Devuelve (True, id_viaje) si funcionó, o (False, mensaje_error) si no."""
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                # Revalidar marchamos DENTRO de la transacción (evita condiciones de
                # carrera entre dos digitadores guardando al mismo tiempo)
                for dest in destinos_viaje:
                    if marchamo_ya_usado(dest["marchamo_ida"], cur):
                        conn.rollback()
                        return False, f"El marchamo '{dest['marchamo_ida']}' ya fue usado en otro viaje."

                id_viaje_str = siguiente_correlativo(cliente, cur)
                fecha_hoy = ahora().strftime("%Y-%m-%d")
                hora_hoy = ahora().strftime("%H:%M:%S")

                cur.execute(
                    "INSERT INTO viajes (id_viaje, cliente, placa, transportista, piloto, auxiliar, "
                    "usuario_creador, fecha_creacion, hora_creacion, cd_origen) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (id_viaje_str, cliente, placa, transportista, piloto, auxiliar, usuario, fecha_hoy, hora_hoy,
                     cd_origen)
                )
                viaje_id = cur.fetchone()[0]

                for i, dest in enumerate(destinos_viaje):
                    cur.execute(
                        "INSERT INTO destinos (viaje_id, orden, tienda, km, galones_base, pedidos, "
                        "marchamo_ida, marchamo_regreso, roles, tarimas, cajas, remitos, incidencias, "
                        "devolucion, creditos, pg_cajas, es_complemento, tipo_pago) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (viaje_id, i + 1, dest["tienda"], dest["km"], dest["galones_base"], dest["pedidos"],
                         dest["marchamo_ida"], dest["marchamo_regreso"] or None,
                         dest["roles"], dest["tarimas"], dest["cajas"],
                         dest.get("remitos", ""), dest.get("incidencias", ""),
                         dest.get("devolucion", ""), dest.get("creditos", ""),
                         dest.get("pg_cajas", 0), dest.get("es_complemento", False),
                         dest.get("tipo_pago", "Local"))
                    )
            conn.commit()
            return True, id_viaje_str
        except psycopg2.IntegrityError as e:
            conn.rollback()
            return False, f"Error de integridad (probablemente un marchamo duplicado): {e}"


def validar_pedido_wms(pedido_codigo):
    """PENDIENTE: aquí se conectará la consulta real al WMS para traer el número de
    cajas despachadas de un pedido. Por ahora no hay conexión configurada, así que
    devuelve None y la app usa el conteo manual que digite el usuario.
    Cuando tengamos el acceso al WMS, esta función deberá devolver un entero
    (cajas reales) o lanzar una excepción si el pedido no existe."""
    return None


def obtener_viajes_recientes(limite=10):
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            "SELECT id_viaje, cliente, placa, piloto, fecha_creacion, hora_creacion, estado "
            "FROM viajes ORDER BY id DESC LIMIT %s", conn, params=(limite,)
        )


def obtener_reporte_bitacora(fecha_inicio, fecha_fin, cliente="Todos"):
    """Un renglón por viaje: la tienda que se muestra es la más lejana (mayor km)
    del viaje, junto con cuántas tiendas llevaba en total. BULTOS = solo cajas."""
    with closing(get_conn()) as conn:
        query = """
            WITH agregado AS (
                SELECT viaje_id,
                       STRING_AGG(marchamo_ida, ' / ' ORDER BY orden) AS marchamos,
                       COUNT(*) AS cantidad_tiendas,
                       SUM(cajas) AS bultos
                FROM destinos
                GROUP BY viaje_id
            ),
            mas_lejano AS (
                SELECT DISTINCT ON (viaje_id) viaje_id, tienda, tipo_pago
                FROM destinos
                ORDER BY viaje_id, km DESC NULLS LAST
            )
            SELECT
                v.fecha_creacion AS "Fecha",
                v.id_viaje AS "No. Despacho",
                v.usuario_creador AS "Supervisor/Coordinador",
                ag.marchamos AS "No. de Marchamo",
                ml.tienda AS "Tienda (más lejana)",
                ag.cantidad_tiendas AS "Cantidad de Tiendas",
                v.placa AS "Placa",
                v.piloto AS "Piloto a Cargo",
                v.cd_origen AS "Origen",
                ml.tipo_pago AS "Clasificación de Destino",
                cam.tipo AS "Tonelaje",
                v.transportista AS "Transportista",
                tr.razon_social AS "Razón Social",
                ag.bultos AS "Bultos (Cajas)"
            FROM viajes v
            JOIN agregado ag ON ag.viaje_id = v.id
            JOIN mas_lejano ml ON ml.viaje_id = v.id
            LEFT JOIN cat_camiones cam ON cam.placa = v.placa
            LEFT JOIN cat_transportistas tr ON tr.nombre = v.transportista
            WHERE v.estado != 'Anulado'
              AND v.fecha_creacion BETWEEN %s AND %s
              AND (%s = 'Todos' OR v.cliente = %s)
            ORDER BY v.fecha_creacion DESC, v.id DESC
        """
        return pd.read_sql_query(query, conn, params=(str(fecha_inicio), str(fecha_fin), cliente, cliente))


def exportar_excel(df):
    buffer = io.BytesIO()
    df.to_excel(buffer, index=False, engine="openpyxl")
    return buffer.getvalue()


def obtener_reporte_liquidaciones(fecha_inicio, fecha_fin, cliente="Todos"):
    """Resumen de cuántos viajes están Pendientes, Liquidados o Anulados en el
    rango de fechas, más el detalle de cada uno para poder ver cuáles faltan."""
    with closing(get_conn()) as conn:
        query = """
            SELECT
                v.fecha_creacion AS "Fecha",
                v.id_viaje AS "No. de Viaje",
                v.cliente AS "Cliente",
                v.placa AS "Placa",
                v.piloto AS "Piloto",
                v.estado AS "Estado",
                v.fecha_liquidacion AS "Fecha de Liquidación",
                v.usuario_liquido AS "Liquidado Por"
            FROM viajes v
            WHERE v.fecha_creacion BETWEEN %s AND %s
              AND (%s = 'Todos' OR v.cliente = %s)
            ORDER BY v.fecha_creacion DESC, v.id DESC
        """
        return pd.read_sql_query(query, conn, params=(str(fecha_inicio), str(fecha_fin), cliente, cliente))


def buscar_viajes(valor_busqueda):
    """Busca viajes por coincidencia PARCIAL (no exacta) de No. de Viaje, Marchamo de
    Ida (de cualquiera de sus destinos), o Placa. Devuelve una lista (puede tener
    más de un resultado si el texto buscado coincide con varios viajes)."""
    valor = f"%{valor_busqueda.strip()}%"
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT v.* FROM viajes v
            WHERE v.id_viaje ILIKE %s OR v.placa ILIKE %s
               OR EXISTS (SELECT 1 FROM destinos d WHERE d.viaje_id = v.id AND d.marchamo_ida ILIKE %s)
            ORDER BY v.id DESC LIMIT 20
        """, (valor, valor, valor))
        return cur.fetchall()


def obtener_destinos_de_viaje(viaje_id):
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM destinos WHERE viaje_id = %s ORDER BY orden", (viaje_id,))
        return cur.fetchall()


def filtrar_viajes(estado="Todos", placa="Todas", limite=50):
    """Filtro rápido por Estado y/o Placa, para encontrar viajes sin tener que
    escribir un texto exacto de búsqueda."""
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM viajes
            WHERE (%s = 'Todos' OR estado = %s)
              AND (%s = 'Todas' OR placa = %s)
            ORDER BY id DESC LIMIT %s
        """, (estado, estado, placa, placa, limite))
        return cur.fetchall()


def anular_viaje(viaje_id, usuario, motivo, liberar_marchamos=True):
    """Marca el viaje como Anulado (no lo borra, queda como registro para auditoría).
    Por defecto libera los marchamos de sus destinos para que puedan reutilizarse en
    otro viaje, ya que un viaje anulado normalmente significa un error de digitación,
    no un marchamo físicamente gastado. Esto es ajustable con liberar_marchamos=False."""
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                fecha_hoy = ahora().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    "UPDATE viajes SET estado='Anulado', usuario_anulo=%s, "
                    "fecha_anulacion=%s, motivo_anulacion=%s WHERE id=%s",
                    (usuario, fecha_hoy, motivo, viaje_id)
                )
                if liberar_marchamos:
                    # Libera los marchamos poniéndolos como usados-pero-anulados con un
                    # sufijo único, para que marchamo_ya_usado() ya no los bloquee.
                    cur.execute(
                        "UPDATE destinos SET marchamo_ida = marchamo_ida || '-ANULADO-' || id::text "
                        "WHERE viaje_id = %s", (viaje_id,)
                    )
            conn.commit()
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, str(e)


def editar_viaje(viaje_id, placa, transportista, piloto, auxiliar, destinos_actualizados, marchamo_regreso_viaje):
    """Corrige los datos de un viaje ya guardado (solo permitido mientras esté
    'Pendiente de Liquidar'). destinos_actualizados es una lista de dicts con el
    id de cada destino y sus campos corregidos. El Marchamo de Regreso es UNO solo
    para todo el viaje — se aplica al destino con el 'orden' más alto y se limpia
    de cualquier otro, para que nunca queden dos tiendas con marchamo de regreso."""
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                # Revalidar que ningún marchamo de ida corregido choque con el de
                # OTRO destino que no sea el mismo que estamos editando.
                for d in destinos_actualizados:
                    cur.execute(
                        "SELECT 1 FROM destinos WHERE marchamo_ida = %s AND id != %s",
                        (d["marchamo_ida"], d["id"])
                    )
                    if cur.fetchone():
                        conn.rollback()
                        return False, f"El marchamo '{d['marchamo_ida']}' ya está en uso en otro destino."

                cur.execute(
                    "UPDATE viajes SET placa=%s, transportista=%s, piloto=%s, auxiliar=%s WHERE id=%s",
                    (placa, transportista, piloto, auxiliar, viaje_id)
                )
                destino_final_id = max(destinos_actualizados, key=lambda d: d["orden"])["id"]
                for d in destinos_actualizados:
                    marchamo_regreso_d = marchamo_regreso_viaje if d["id"] == destino_final_id else None
                    cur.execute(
                        "UPDATE destinos SET tienda=%s, roles=%s, tarimas=%s, cajas=%s, marchamo_ida=%s, "
                        "marchamo_regreso=%s, remitos=%s, devolucion=%s, creditos=%s, pg_cajas=%s, "
                        "incidencias=%s, es_complemento=%s, tipo_pago=%s WHERE id=%s",
                        (d["tienda"], d["roles"], d["tarimas"], d["cajas"], d["marchamo_ida"],
                         marchamo_regreso_d, d["remitos"], d["devolucion"], d["creditos"],
                         d["pg_cajas"], d["incidencias"], d["es_complemento"], d["tipo_pago"], d["id"])
                    )
            conn.commit()
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, str(e)


def generar_hoja_control_html(viaje, destinos):
    """Arma la Hoja de Control de Viaje como HTML: en pantalla se ve con los colores
    de Ransa, pero al imprimir (@media print) los fondos de color se vuelven blancos
    y solo quedan bordes negros, para que salga limpia en una impresora blanco y negro.
    Si el viaje ya está Liquidado, los recuadros de devolución se llenan con los datos
    reales de la liquidación en vez de salir en blanco."""
    marchamo_ida_general = destinos[0]["marchamo_ida"] if destinos else ""
    es_cliente_unisuper = viaje["cliente"].startswith("UniSuper")
    esta_liquidado = viaje["estado"] == "Liquidado"

    bloques_destino = ""
    for idx, d in enumerate(destinos, start=1):
        try:
            pedidos_lista = json.loads(d["pedidos"]) if d["pedidos"] else []
        except (json.JSONDecodeError, TypeError):
            pedidos_lista = []
        pedidos_txt = ", ".join(p["pedido"] for p in pedidos_lista) if pedidos_lista else "—"
        badge_regreso = (
            f'<span class="badge-regreso">Marchamo Retorno: {d["marchamo_regreso"]}</span>'
            if d["marchamo_regreso"] else ""
        )
        badge_complemento = '<span class="badge-complemento">COMPLEMENTO</span>' if d["es_complemento"] else ""
        incidencia_txt = d["incidencias"] or ""
        incidencia_html = f'<div class="incidencia">⚠ {incidencia_txt}</div>' if incidencia_txt else ""
        material_cajas = (
            f'<div class="material-box"><b>{d["cajas"]}</b><span>CAJAS</span></div>'
            if not d["es_complemento"] else ""
        )
        documentos_html = (
            f'<div class="sub-info">Remisión: <b>{d["remitos"] or "—"}</b> &nbsp;|&nbsp;'
            f'Devolución: <b>{d["devolucion"] or "—"}</b> &nbsp;|&nbsp;'
            f'Créditos: <b>{d["creditos"] or "—"}</b> &nbsp;|&nbsp;'
            f'Cartas Solicitud Producto P&amp;G: <b>{d["pg_cajas"] or 0}</b></div>'
            if es_cliente_unisuper else ""
        )

        if esta_liquidado:
            devuelto_boxes = f"""
                        <div class="material-box"><b>{d["roles_devueltos"] or 0}</b></div>
                        <div class="material-box"><b>{d["tarimas_devueltas"] or 0}</b></div>
                        <div class="material-box"><b>{d["pacas_carton_devueltas"] or 0}</b></div>"""
        else:
            devuelto_boxes = """
                        <div class="material-box vacio"></div>
                        <div class="material-box vacio"></div>
                        <div class="material-box vacio"></div>"""

        bloques_destino += f"""
        <div class="destino-card">
            <div class="destino-header">
                <span class="destino-num">{idx}</span> {d['tienda']}
                <span class="marchamo-inline">🔒 {d['marchamo_ida']}</span>
                {badge_complemento}
                {badge_regreso}
            </div>
            <div class="destino-body">
                <div class="material-enviado">
                    <div class="etiqueta">MATERIAL ENVIADO</div>
                    <div class="material-grid">
                        <div class="material-box"><b>{d['roles']}</b><span>ROLES</span></div>
                        <div class="material-box"><b>{d['tarimas']}</b><span>TARIMAS</span></div>
                        {material_cajas}
                    </div>
                    <div class="sub-info sub-info-grande">No. de Pedidos: <b>{pedidos_txt}</b></div>
                    {documentos_html}
                    {incidencia_html}
                    <div class="sello-area">
                        <span class="etiqueta">SELLO DE TIENDA</span>
                        <div class="sello-espacio"></div>
                    </div>
                </div>
                <div class="material-devuelto">
                    <div class="etiqueta">{"DEVUELTO POR LA TIENDA (LIQUIDADO)" if esta_liquidado else "DEVUELTO POR LA TIENDA (LLENAR A MANO)"}</div>
                    <div class="grid-encabezados">
                        <span>ROLES</span><span>TARIMAS</span><span>PACAS CARTÓN</span>
                    </div>
                    <div class="material-grid">{devuelto_boxes}
                    </div>
                    <div class="etiqueta" style="margin-top:10px;">CONTROL DE TIEMPOS (LLENAR A MANO)</div>
                    <div class="grid-encabezados">
                        <span>LLEGADA</span><span>INICIO DESCARGA</span><span>SALIDA</span>
                    </div>
                    <div class="material-grid">
                        <div class="material-box vacio"></div>
                        <div class="material-box vacio"></div>
                        <div class="material-box vacio"></div>
                    </div>
                </div>
            </div>
        </div>
        """

    return f"""
    <html>
    <head>
    <style>
        @page {{ size: letter; margin: 10mm; }}
        body {{ font-family: Arial, sans-serif; color: #1a1a1a; padding: 14px; font-size: 13px; }}
        .hoja {{ max-width: 800px; margin: auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: flex-start;
                   border-bottom: 3px solid #0B4A32; padding-bottom: 6px; margin-bottom: 8px; }}
        .logo {{ font-size: 22px; font-weight: bold; color: #0B4A32; }}
        .titulo {{ text-align: right; }}
        .titulo h2 {{ margin: 0; color: #0B4A32; font-size: 16px; }}
        .titulo p {{ margin: 0; color: #666; font-size: 10px; }}
        .datos-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 5px 16px; margin-bottom: 8px; }}
        .dato label {{ display: block; font-size: 9px; color: #666; text-transform: uppercase; }}
        .dato span {{ font-size: 12px; font-weight: bold; border-bottom: 1px solid #ccc; display: block; }}
        .dato-blanco span {{ border-bottom: 1px dashed #999; min-height: 14px; }}
        .titulo-destinos {{ background: #0B4A32; color: white; padding: 3px 10px; font-weight: bold;
                             border-radius: 4px; margin-bottom: 6px; font-size: 12px; }}
        .destino-card {{ border: 1px solid #ccc; border-radius: 5px; margin-bottom: 6px; overflow: hidden; }}
        .destino-header {{ background: #eef6ef; padding: 4px 10px; font-weight: bold; position: relative; font-size: 12px; }}
        .destino-num {{ background: #0B4A32; color: white; border-radius: 50%; padding: 1px 7px; margin-right: 5px; font-size: 11px; }}
        .marchamo-inline {{ margin-left: 10px; font-size: 11px; font-weight: 600; color: #0B4A32;
                             background: #fff; border: 1px solid #0B4A32; border-radius: 4px; padding: 1px 8px; }}
        .badge-regreso {{ float: right; background: #B5622E; color: white; padding: 3px 12px;
                           border-radius: 4px; font-size: 13px; font-weight: bold; }}
        .badge-complemento {{ background: #7A3E1D; color: white; padding: 1px 8px;
                               border-radius: 4px; font-size: 10px; margin-left: 6px; }}
        .destino-body {{ display: flex; }}
        .material-enviado, .material-devuelto {{ flex: 1; padding: 5px 8px; }}
        .material-devuelto {{ border-left: 2px dashed #ccc; }}
        .etiqueta {{ font-size: 9px; color: #0B4A32; font-weight: bold; margin-bottom: 3px; }}
        .material-grid {{ display: flex; gap: 5px; }}
        .material-box {{ flex: 1; border: 1px solid #ccc; border-radius: 4px; text-align: center; padding: 3px 2px; }}
        .material-box b {{ display: block; font-size: 13px; color: #0B4A32; }}
        .material-box.vacio {{ min-height: 22px; }}
        .material-box span {{ font-size: 8px; color: #666; }}
        .sub-info {{ font-size: 10px; margin-top: 3px; }}
        .sub-info-grande {{ font-size: 13px; margin-top: 5px; font-weight: 600; }}
        .grid-encabezados {{ display: flex; gap: 5px; margin-top: 4px; }}
        .grid-encabezados span {{ flex: 1; text-align: center; font-size: 8px; color: #666; text-transform: uppercase; }}
        .sello-area {{ margin-top: 10px; }}
        .sello-espacio {{ min-height: 46px; }}
        .incidencia {{ font-size: 10px; margin-top: 3px; color: #b34700; }}
        .footer {{ display: flex; justify-content: space-between; font-size: 9px; color: #999; margin-top: 10px; }}
        .btn-imprimir {{ background: #0B4A32; color: white; border: none; padding: 10px 20px;
                          border-radius: 6px; font-weight: bold; cursor: pointer; margin-bottom: 15px; }}

        @media print {{
            body {{ padding: 0; font-size: 11px; }}
            .btn-imprimir {{ display: none; }}
            .logo, .titulo h2, .dato span, .etiqueta, .destino-num, .material-box b {{ color: #000 !important; }}
            .titulo-destinos, .destino-header, .destino-num {{ background: #fff !important; border: 1px solid #000; color: #000 !important; }}
            .marchamo-inline {{ border: 1px solid #000; color: #000 !important; background: #fff !important; }}
            .badge-regreso {{ background: #fff !important; color: #000 !important; border: 1px solid #000; }}
            .badge-complemento {{ background: #fff !important; color: #000 !important; border: 1px solid #000; }}
            .material-box {{ border: 1px solid #000; }}
            .destino-card {{ break-inside: avoid; }}
        }}
    </style>
    </head>
    <body>
    <div class="hoja">
        <button class="btn-imprimir" onclick="window.print()">🖨️ Imprimir</button>
        <div class="header">
            <div class="logo">RANSA</div>
            <div class="titulo">
                <h2>HOJA DE SALIDA</h2>
                <p>Control de Ruta · Documento de Despacho</p>
                <p style="margin-top:4px;">Generado: <b>{viaje['fecha_creacion']} {viaje['hora_creacion']}</b>
                   por <b>{viaje['usuario_creador']}</b></p>
                {f'<p style="margin-top:2px; color:#0B4A32;">✅ Liquidado por <b>{viaje["usuario_liquido"]}</b> el <b>{viaje["fecha_liquidacion"]} {viaje["hora_liquidacion"]}</b></p>' if esta_liquidado else ''}
            </div>
        </div>
        <div class="datos-grid">
            <div class="dato"><label>No. de Viaje</label><span>{viaje['id_viaje']}</span></div>
            <div class="dato"><label>Cliente</label><span>{viaje['cliente']}</span></div>
            <div class="dato"><label>CD Origen</label><span>{viaje['cd_origen'] or '—'}</span></div>
            <div class="dato"><label>Transportista</label><span>{viaje['transportista']}</span></div>
            <div class="dato"><label>Placa</label><span>{viaje['placa']}</span></div>
            <div class="dato"><label>Fecha</label><span>{viaje['fecha_creacion']}</span></div>
            <div class="dato dato-blanco"><label>Horario (Garita — hora real de salida)</label><span>&nbsp;</span></div>
            <div class="dato"><label>Piloto</label><span>{viaje['piloto']}</span></div>
            <div class="dato"><label>Auxiliar</label><span>{viaje['auxiliar']}</span></div>
            <div class="dato"><label>Marchamo de Ida</label><span>{marchamo_ida_general}</span></div>
        </div>
        <div class="titulo-destinos">DESTINOS DEL VIAJE ({len(destinos)})</div>
        {bloques_destino}
        <div class="footer">
            <span>Hoja generada por el sistema Control de Ruta · Ransa</span>
            <span>Sellar y entregar al finalizar el viaje para su liquidación.</span>
        </div>
    </div>
    </body>
    </html>
    """


def liquidar_viaje(viaje_id, destinos_actualizados, usuario):
    """Registra lo que el camión trajo de regreso por cada destino y marca el viaje como Liquidado.
    destinos_actualizados: lista de dicts con id, roles_devueltos, tarimas_devueltas, pacas_carton_devueltas."""
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                for d in destinos_actualizados:
                    cur.execute(
                        "UPDATE destinos SET roles_devueltos=%s, tarimas_devueltas=%s, "
                        "pacas_carton_devueltas=%s WHERE id=%s",
                        (d["roles_devueltos"], d["tarimas_devueltas"], d["pacas_carton_devueltas"], d["id"])
                    )
                fecha_hoy = ahora().strftime("%Y-%m-%d")
                hora_hoy = ahora().strftime("%H:%M:%S")
                cur.execute(
                    "UPDATE viajes SET estado='Liquidado', usuario_liquido=%s, "
                    "fecha_liquidacion=%s, hora_liquidacion=%s WHERE id=%s",
                    (usuario, fecha_hoy, hora_hoy, viaje_id)
                )
            conn.commit()
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, str(e)


# ==========================================
# CATÁLOGOS MAESTROS — ahora viven en la base de datos (tablas cat_*), no en el
# código. Se cargan una vez por sesión; la pantalla "⚙️ Catálogos" permite
# subir un Excel para reemplazarlos sin tocar una sola línea de código.
# ==========================================
if 'catalogos' not in st.session_state:
    st.session_state.catalogos = cargar_catalogos_desde_db()

# Contador de "corridas" del formulario: se incrementa después de guardar un
# viaje para que los widgets nazcan con keys nuevas (así el formulario queda
# limpio para el siguiente viaje, en vez de arrastrar lo digitado antes).
if 'form_run' not in st.session_state:
    st.session_state.form_run = 0
if 'num_destinos' not in st.session_state:
    st.session_state.num_destinos = 1

# ==========================================
# SIDEBAR + FLUJO DE ENTRADA (Login → Cliente/CD → App)
# ==========================================
st.sidebar.markdown("<h2 style='color:#0B4A32; font-weight:700; letter-spacing:-0.02em;'>RANSA</h2>", unsafe_allow_html=True)
st.sidebar.caption("Control de Ruta")

st.sidebar.markdown("---")
try:
    with closing(get_conn()):
        pass
except Exception as e:
    st.sidebar.error(f"🔴 Sin conexión a la base de datos: {e}")
    st.stop()

# --- PANTALLA 1: LOGIN ---
# Todavía no valida contraseña (eso queda pendiente para cuando resolvamos
# seguridad de verdad), pero exige un clic físico explícito para entrar — no
# avanza solo. Sirve como primera barrera visual mientras estamos en User Test.
if not st.session_state.get("login_confirmado"):
    st.markdown("""
        <div class="ransa-topbar" style="justify-content:center;">
            <div style="text-align:center;">
                <div class="titulo">RANSA <span style="font-weight:400;">· Sistema de Control de Viajes</span></div>
                <div class="subtitulo">Ingresa con tu usuario para continuar</div>
            </div>
        </div>
    """, unsafe_allow_html=True)
    col_izq, col_centro, col_der = st.columns([1, 1.2, 1])
    with col_centro:
        with st.container(border=True):
            st.markdown("#### :material/lock: Iniciar Sesión")
            usuario_login = st.selectbox("Usuario", list(st.session_state.catalogos["usuarios"].keys()))
            st.caption(f"Perfil: **{st.session_state.catalogos['usuarios'][usuario_login]}**")
            if st.button(":material/login: Ingresar al Sistema", use_container_width=True):
                st.session_state["usuario_activo_fijo"] = usuario_login
                st.session_state["login_confirmado"] = True
                st.rerun()
    st.caption("⚠️ Validación de usuario simulada — falta la contraseña real, pendiente para cuando resolvamos seguridad.")
    st.stop()

usuario_activo = st.session_state["usuario_activo_fijo"]
perfil_activo = st.session_state.catalogos["usuarios"][usuario_activo]

st.sidebar.success(f"👤 **{usuario_activo}**")
st.sidebar.caption(f"Perfil: {perfil_activo}")
if st.sidebar.button(":material/logout: Cerrar Sesión"):
    st.session_state["login_confirmado"] = False
    st.session_state["config_bloqueada"] = False
    del st.session_state["usuario_activo_fijo"]
    for k in ("cliente_activo_fijo", "cd_origen_fijo"):
        st.session_state.pop(k, None)
    st.rerun()

# --- PANTALLA 2: Cliente y CD Origen — se eligen UNA SOLA VEZ por sesión. Para
# cambiarlos hay que cerrar sesión y volver a entrar (evita que a mitad de una
# jornada alguien cambie sin querer el cliente/CD y se mezclen viajes).
if not st.session_state.get("config_bloqueada"):
    st.sidebar.markdown("---")
    st.markdown("""
        <div class="ransa-topbar" style="justify-content:center;">
            <div style="text-align:center;">
                <div class="titulo">🎯 Selección de Cliente y CD Origen</div>
                <div class="subtitulo">Confirma para comenzar a trabajar</div>
            </div>
        </div>
    """, unsafe_allow_html=True)

    clientes_disp = clientes_permitidos_para(usuario_activo, perfil_activo)
    if not clientes_disp:
        st.error("🚫 Tu usuario no tiene ningún cliente asignado. Pídele a un Administrador que te "
                 "dé acceso desde la pestaña de Catálogos ('Acceso Usuario → Cliente').")
        st.stop()

    cliente_activo_sel = st.sidebar.selectbox("🎯 Cliente", clientes_disp)

    cds_disponibles = st.session_state.catalogos["cds_por_cliente"].get(cliente_activo_sel, [])
    if len(cds_disponibles) <= 1:
        cd_origen_sel = cds_disponibles[0] if cds_disponibles else ""
        st.sidebar.info(f"CD Origen (único, automático): **{cd_origen_sel}**")
    else:
        cd_origen_sel = st.sidebar.selectbox("CD Origen", cds_disponibles)

    if st.sidebar.button(":material/check_circle: Confirmar y Comenzar a Trabajar"):
        st.session_state["cliente_activo_fijo"] = cliente_activo_sel
        st.session_state["cd_origen_fijo"] = cd_origen_sel
        st.session_state["config_bloqueada"] = True
        st.rerun()
    st.info("👈 Selecciona el Cliente (y el CD Origen si el cliente tiene más de uno) en la "
            "barra lateral, y confirma para comenzar a trabajar.")
    st.stop()

cliente_activo = st.session_state["cliente_activo_fijo"]
cd_origen_fijo = st.session_state["cd_origen_fijo"]

st.sidebar.markdown("---")
st.sidebar.success(f"🎯 **{cliente_activo}** · CD {cd_origen_fijo}")
if st.sidebar.button(":material/swap_horiz: Cambiar Cliente / CD"):
    st.session_state["config_bloqueada"] = False
    del st.session_state["cliente_activo_fijo"]
    del st.session_state["cd_origen_fijo"]
    st.rerun()
st.sidebar.caption("⚠️ Si tienes un viaje a medio llenar sin guardar, se pierde al cambiar de cliente.")

st.markdown(f"""
    <div class="ransa-topbar">
        <div>
            <div class="titulo">RANSA <span style="font-weight:400;">· Control de Ruta</span>
                <span class="ransa-badge">TMS</span>
            </div>
            <div class="subtitulo">Sistema Integral de Gestión Logística</div>
        </div>
        <div class="contexto">
            <b>{cliente_activo}</b> · CD {cd_origen_fijo}<br>
            {usuario_activo} ({perfil_activo}) · {ahora().strftime('%H:%M:%S')}
        </div>
    </div>
""", unsafe_allow_html=True)
st.markdown("---")

tab1, tab2, tab5, tab3, tab4 = st.tabs([
    ":material/local_shipping: Despacho (Salidas)",
    ":material/receipt_long: Recepción (Liquidaciones)",
    ":material/edit_document: Gestión de Viajes",
    ":material/bar_chart: Reportes",
    ":material/settings: Catálogos"
])

# ==========================================
# MÓDULO 1: DESPACHO / CREACIÓN DE VIAJES
# ==========================================
with tab1:
    if perfil_activo in ["Administrador", "Operador"]:
        st.header(":material/local_shipping: Creación de Viaje")
        st.caption(f"Configura placa, ruta y materiales del nuevo viaje · Digitando como **{usuario_activo}** ({perfil_activo})")

        run = st.session_state.form_run  # sufijo de las keys del formulario actual
        marchamo_regreso_actual = st.session_state.get(f"mreg_final_{run}", "")
        tiendas_cliente = st.session_state.catalogos["clientes"][cliente_activo]
        es_cliente_unisuper = cliente_activo.startswith("UniSuper")

        col_main, col_side = st.columns([2.2, 1], gap="medium")

        with col_main:
            with st.container(border=True):
                st.markdown("##### :material/badge: INFORMACIÓN DEL VIAJE")
                ic1, ic2, ic3 = st.columns(3)
                ic1.text_input("Cliente Operativo", value=cliente_activo, disabled=True, key=f"info_cli_{run}")
                ic2.text_input("Correlativo de Viaje (automático)", value=peek_siguiente_correlativo(cliente_activo), disabled=True, key=f"info_corr_{run}")
                ic3.text_input("CD Origen", value=cd_origen_fijo, disabled=True, key=f"info_cd_{run}")

                col_p, col_t, col_cap, col_pil, col_aux = st.columns(5)
                with col_p:
                    placa = st.selectbox("Placa del Camión", [""] + list(st.session_state.catalogos["camiones"].keys()), key=f"placa_{run}")

                t_pred, cap_pred, pil_pred, aux_pred = "", "", "", ""
                if placa:
                    datos_c = st.session_state.catalogos["camiones"][placa]
                    t_pred = datos_c["transportista"]
                    cap_pred = datos_c["tipo"]
                    pil_pred = datos_c["piloto"]
                    aux_pred = datos_c["auxiliar"]

                with col_t:
                    st.text_input("Transportista", value=t_pred, disabled=True, key=f"transp_{run}")
                with col_cap:
                    st.text_input("Capacidad Camión", value=cap_pred, disabled=True, key=f"cap_{run}")
                with col_pil:
                    pilotos = st.session_state.catalogos["pilotos"]
                    piloto_final = st.selectbox("Piloto", pilotos, index=pilotos.index(pil_pred) if pil_pred in pilotos else 0, key=f"piloto_{run}")
                with col_aux:
                    auxiliares = st.session_state.catalogos["auxiliares"]
                    auxiliar_final = st.selectbox("Auxiliar de Carga", auxiliares, index=auxiliares.index(aux_pred) if aux_pred in auxiliares else 0, key=f"aux_{run}")

            cd_origen_final = cd_origen_fijo

            with st.container(border=True):
                st.markdown("##### :material/route: RUTA Y DESTINOS")
                st.caption("Cuenta lo físico primero; el marchamo de ida se cierra al final de cada tienda.")

                destinos_viaje = []
                total_destinos = st.session_state.num_destinos
                tiendas_usadas_en_form = set()

                for i in range(total_destinos):
                    key_subrun_pedido = f"pedido_subrun_{run}_{i}"
                    if key_subrun_pedido not in st.session_state:
                        st.session_state[key_subrun_pedido] = 0
                    subrun = st.session_state[key_subrun_pedido]

                    key_lista_pedidos = f"pedidos_lista_{run}_{i}"
                    if key_lista_pedidos not in st.session_state:
                        st.session_state[key_lista_pedidos] = []
                    lista_pedidos = st.session_state[key_lista_pedidos]

                    with st.container(border=True):
                        cab1, cab2, cab3, cab4 = st.columns([0.35, 2.2, 1.5, 0.4])
                        cab1.markdown(f'<div class="badge-numero">{i + 1}</div>', unsafe_allow_html=True)
                        with cab2:
                            tienda = st.selectbox("Tienda / Destino", [""] + list(tiendas_cliente.keys()),
                                                   key=f"t_{run}_{i}", label_visibility="collapsed",
                                                   placeholder="Tienda / Destino")
                        with cab3:
                            m_ida_tienda = st.text_input("Marchamo Ida", key=f"mida_{run}_{i}",
                                                          label_visibility="collapsed", placeholder="Marchamo Ida")
                        with cab4:
                            puede_borrar = (i == total_destinos - 1) and total_destinos > 1
                            if st.button(":material/delete:", key=f"del_destino_{run}_{i}", disabled=not puede_borrar,
                                         help="Quitar este destino" if puede_borrar else "Solo puedes quitar el último destino agregado"):
                                st.session_state.num_destinos -= 1
                                st.rerun()

                        km_t = tiendas_cliente[tienda]["km"] if tienda else 0.0
                        rendimiento_camion = st.session_state.catalogos["rendimiento"].get(cap_pred)
                        es_complemento = st.toggle("¿Es complemento? (resto de un pedido que no cupo antes)", key=f"comp_{run}_{i}")
                        gal_t = round(km_t / rendimiento_camion, 2) if (tienda and rendimiento_camion) else 0.0
                        if tienda:
                            st.caption(f"Distancia: {km_t} KM"
                                       + (" · Complemento: solo Roles y Tarimas." if es_complemento else ""))

                        with st.expander("Detalle de cajas, material y documentos", expanded=True):
                            if not es_complemento:
                                fp1, fp2, fp3 = st.columns([1.6, 1, 1])
                                with fp1:
                                    pedido_codigo = st.text_input("No. de Pedido", key=f"cod_pedido_{run}_{i}_{subrun}")
                                with fp2:
                                    pedido_cajas = st.number_input("Cajas del pedido", min_value=0, step=1, value=None, placeholder="0", key=f"cajas_pedido_{run}_{i}_{subrun}") or 0
                                with fp3:
                                    st.write("")
                                    agregar_pedido = st.button(":material/add: Agregar Pedido", key=f"btn_agregar_pedido_{run}_{i}_{subrun}", use_container_width=True)
                                if agregar_pedido:
                                    if not pedido_codigo.strip():
                                        st.warning("Escribe un número de pedido antes de agregarlo.")
                                    elif pedido_cajas <= 0:
                                        st.warning("⚠️ Ese pedido no tiene cajas — indica cuántas cajas trae antes de agregarlo.")
                                    else:
                                        cajas_wms = validar_pedido_wms(pedido_codigo.strip())
                                        lista_pedidos.append({
                                            "pedido": pedido_codigo.strip(),
                                            "cajas": cajas_wms if cajas_wms is not None else pedido_cajas,
                                            "origen": "WMS" if cajas_wms is not None else "Manual"
                                        })
                                        st.session_state[key_subrun_pedido] += 1
                                        st.rerun()

                                if lista_pedidos:
                                    for idx, p in enumerate(lista_pedidos):
                                        pc1, pc2, pc3, pc4 = st.columns([2, 1, 1, 1])
                                        pc1.write(f":material/description: {p['pedido']}")
                                        pc2.write(f"{p['cajas']} cajas")
                                        pc3.write(f"_{p['origen']}_")
                                        if pc4.button(":material/delete:", key=f"del_pedido_{run}_{i}_{idx}"):
                                            lista_pedidos.pop(idx)
                                            st.rerun()
                            else:
                                cajas_total = 0

                            mf1, mf2, mf3, mf4 = st.columns(4)
                            with mf1:
                                if es_complemento:
                                    st.number_input("Cajas Totales", value=0, disabled=True, key=f"c_disabled_{run}_{i}")
                                elif lista_pedidos:
                                    cajas_total = sum(p["cajas"] for p in lista_pedidos)
                                    st.number_input("Cajas Totales", value=cajas_total, disabled=True, key=f"c_calc_{run}_{i}")
                                else:
                                    cajas_total = st.number_input("Cajas Totales", min_value=0, step=1, value=None, placeholder="0", key=f"c_{run}_{i}") or 0
                            with mf2:
                                tarimas = st.number_input("Tarimas", min_value=0, step=1, value=None, placeholder="0", key=f"tar_{run}_{i}") or 0
                            with mf3:
                                roles = st.number_input("Roles Secos", min_value=0, step=1, value=None, placeholder="0", key=f"r_{run}_{i}") or 0
                            with mf4:
                                tipo_pago = st.selectbox("Clasificación de Destino", ["Local", "Departamental"], key=f"tipopago_{run}_{i}")

                            if es_cliente_unisuper:
                                dc1, dc2, dc3, dc4 = st.columns(4)
                                with dc1:
                                    remitos_txt = st.text_input("Remisión", key=f"remitos_{run}_{i}", max_chars=10, placeholder="10 caracteres")
                                with dc2:
                                    devolucion_txt = st.text_input("Devolución", key=f"dev_{run}_{i}", max_chars=10, placeholder="10 caracteres")
                                with dc3:
                                    creditos_txt = st.text_input("Créditos", key=f"cred_{run}_{i}", max_chars=10, placeholder="10 caracteres")
                                with dc4:
                                    if not es_complemento:
                                        pg_cajas = st.number_input("Cartas Sol. P&G", min_value=0, step=1, value=None, placeholder="0", key=f"pg_{run}_{i}") or 0
                                    else:
                                        pg_cajas = 0
                            else:
                                pg_cajas = 0
                                remitos_txt = ""
                                devolucion_txt = ""
                                creditos_txt = ""

                            observaciones_txt = st.text_area(
                                "Observaciones", key=f"obs_{run}_{i}",
                                placeholder="Ej: lleva transferencia T-123, tienda cerrada, faltante detectado, etc."
                            )

                        if tienda:
                            if tienda in tiendas_usadas_en_form:
                                st.warning(f"⚠️ La tienda '{tienda}' ya está agregada como otro destino de este mismo viaje.")
                            tiendas_usadas_en_form.add(tienda)
                            destinos_viaje.append({
                                "tienda": tienda,
                                "km": km_t,
                                "galones_base": gal_t,
                                "pedidos": json.dumps(lista_pedidos),
                                "marchamo_ida": m_ida_tienda.strip(),
                                "marchamo_regreso": "",  # se completa en el panel de cierre
                                "roles": roles,
                                "tarimas": tarimas,
                                "cajas": cajas_total,
                                "remitos": remitos_txt.strip(),
                                "incidencias": observaciones_txt.strip(),
                                "devolucion": devolucion_txt.strip(),
                                "creditos": creditos_txt.strip(),
                                "pg_cajas": pg_cajas,
                                "es_complemento": es_complemento,
                                "tipo_pago": tipo_pago
                            })

                if st.button(":material/add: Agregar Parada"):
                    st.session_state.num_destinos += 1
                    st.rerun()

        with col_side:
            with st.container(border=True):
                st.markdown("##### :material/summarize: RESUMEN DE DATOS")
                total_tarimas = sum(d["tarimas"] for d in destinos_viaje)
                total_roles = sum(d["roles"] for d in destinos_viaje)
                total_cajas = sum(d["cajas"] for d in destinos_viaje)
                cantidad_tiendas = len(destinos_viaje)
                distancia_total = round(sum(d["km"] for d in destinos_viaje), 1)

                mcol1, mcol2 = st.columns(2)
                mcol1.metric("Cajas", total_cajas)
                mcol2.metric("Tarimas", total_tarimas)
                mcol1.metric("Roles", total_roles)
                mcol2.metric("Cantidad de Tiendas", cantidad_tiendas)
                mcol1.metric("Distancia Total (KM)", distancia_total)

                st.markdown("---")
                st.caption(":material/preview: VISTA PREVIA — HOJA DE SALIDA")
                if not destinos_viaje:
                    st.caption("Agrega un destino para ver la vista previa.")
                else:
                    for idx, d in enumerate(destinos_viaje, start=1):
                        st.markdown(f"**{idx}. {d['tienda'] or '—'}**")
                        st.caption(f"Marchamo Ida: {d['marchamo_ida'] or '—'}")
                        st.caption(f"Tarimas: {d['tarimas']} · Roles: {d['roles']} · Cajas: {d['cajas']}")
                        if idx == len(destinos_viaje) and marchamo_regreso_actual:
                            st.markdown(f":material/lock: **Marchamo Regreso:** {marchamo_regreso_actual}")
                        st.markdown("---")

        st.markdown("---")
        with st.container(border=True):
            st.markdown("##### :material/lock: CIERRE DEL VIAJE")
            st.caption("Marchamo de Regreso — se coloca cuando ya se cerraron todas las tiendas.")
            cc1, cc2 = st.columns([2, 1])
            with cc1:
                marchamo_regreso_viaje = st.text_input("Marchamo de REGRESO (obligatorio)", key=f"mreg_final_{run}", label_visibility="collapsed", placeholder="Marchamo de Regreso")
            if destinos_viaje:
                destinos_viaje[-1]["marchamo_regreso"] = marchamo_regreso_viaje.strip()
            with cc2:
                guardar_click = st.button(":material/print: Generar Viaje e Imprimir", use_container_width=True, type="primary")

        if guardar_click:
            marchamos_vacios = any(not d["marchamo_ida"] for d in destinos_viaje)
            marchamos_repetidos_en_form = len([d["marchamo_ida"] for d in destinos_viaje]) != len(
                set(d["marchamo_ida"] for d in destinos_viaje)
            )

            if not placa or len(destinos_viaje) == 0:
                st.error("❌ Error: Debe seleccionar el camión y al menos un destino.")
            elif marchamos_vacios:
                st.error("❌ Error: Todos los destinos ingresados deben tener un Marchamo de Ida asignado.")
            elif marchamos_repetidos_en_form:
                st.error("❌ Error: Hay marchamos de ida repetidos dentro de este mismo viaje.")
            elif not marchamo_regreso_viaje.strip():
                st.error("❌ Error: El Marchamo de Regreso es obligatorio para cerrar el circuito.")
            else:
                ok, resultado = guardar_viaje(
                    cliente=cliente_activo,
                    placa=placa,
                    transportista=t_pred,
                    piloto=piloto_final,
                    auxiliar=auxiliar_final,
                    usuario=usuario_activo,
                    destinos_viaje=destinos_viaje,
                    cd_origen=cd_origen_final
                )
                if ok:
                    st.success(f"✅ Viaje {resultado} guardado correctamente.")
                    # El camión "aprende" el piloto/auxiliar usado esta vez, para
                    # que la próxima vez ya salga como default (se puede cambiar).
                    actualizar_default_camion(placa, piloto_final, auxiliar_final)
                    st.session_state.catalogos = cargar_catalogos_desde_db()
                    st.session_state.num_destinos = 1
                    st.session_state.form_run += 1  # limpia el formulario para el próximo viaje
                    st.session_state["ultimo_viaje_guardado"] = resultado
                    st.rerun()
                else:
                    st.error(f"❌ Error: {resultado}")

        if st.session_state.get("ultimo_viaje_guardado"):
            st.markdown("---")
            if st.button(f"🖨️ Ver Hoja de Control del viaje {st.session_state['ultimo_viaje_guardado']}"):
                resultados = buscar_viajes(st.session_state["ultimo_viaje_guardado"])
                if resultados:
                    v = resultados[0]
                    d = obtener_destinos_de_viaje(v["id"])
                    components.html(generar_hoja_control_html(v, d), height=900, scrolling=True)

        st.markdown("### 🕒 Últimos viajes registrados")
        st.dataframe(obtener_viajes_recientes(), use_container_width=True, height=280)
    else:
        st.info("Tu perfil no tiene permisos para despachar viajes.")

# ==========================================
# MÓDULO 2: LIQUIDACIONES
# ==========================================
with tab2:
    if perfil_activo in ["Administrador", "Liquidador"]:
        st.header(":material/receipt_long: Liquidación de Viajes")
        st.caption("Registra lo que el camión trajo de regreso de cada tienda. Las cajas no se devuelven.")

        with st.expander("🔍 Filtros rápidos (sin escribir nada)", expanded=False):
            fcol1, fcol2, fcol3 = st.columns([1, 1, 0.6])
            with fcol1:
                filtro_estado_liq = st.selectbox(
                    "Estado", ["Pendiente de Liquidar", "Todos", "Liquidado", "Anulado"], key="filtro_estado_liq"
                )
            with fcol2:
                placas_filtro = ["Todas"] + list(st.session_state.catalogos["camiones"].keys())
                filtro_placa_liq = st.selectbox("Placa", placas_filtro, key="filtro_placa_liq")
            with fcol3:
                st.write("")
                filtrar_click = st.button(":material/filter_alt: Filtrar", use_container_width=True, key="btn_filtrar_liq")

            if filtrar_click:
                st.session_state["filtrados_liq"] = filtrar_viajes(filtro_estado_liq, filtro_placa_liq)

            filtrados = st.session_state.get("filtrados_liq", [])
            if not filtrados:
                st.caption("Ajusta los filtros y presiona 'Filtrar'.")
            else:
                for p in filtrados:
                    pcol1, pcol2, pcol3, pcol4, pcol5 = st.columns([2, 2, 2, 2, 1])
                    pcol1.write(f"**{p['id_viaje']}**")
                    pcol2.write(p["cliente"])
                    pcol3.write(p["placa"])
                    pcol4.write(p["estado"])
                    if pcol5.button("Elegir", key=f"elegir_filtro_{p['id']}"):
                        st.session_state["viaje_liq"] = p
                        st.session_state["destinos_liq"] = obtener_destinos_de_viaje(p["id"])
                        st.rerun()

        col_val, col_btn = st.columns([4, 1])
        with col_val:
            valor_busqueda = st.text_input(
                "Buscar por No. de Viaje, Marchamo de Ida o Placa (no hace falta escribirlo completo)",
                key="valor_liq"
            )
        with col_btn:
            st.write("")
            buscar = st.button(":material/search: Buscar", use_container_width=True)

        if buscar:
            if valor_busqueda.strip():
                resultados = buscar_viajes(valor_busqueda)
                st.session_state["resultados_busqueda_liq"] = resultados
                st.session_state.pop("viaje_liq", None)
                st.session_state.pop("destinos_liq", None)
            else:
                st.warning("Escribe algo para buscar.")

        resultados = st.session_state.get("resultados_busqueda_liq", [])
        if resultados and "viaje_liq" not in st.session_state:
            if len(resultados) == 1:
                st.session_state["viaje_liq"] = resultados[0]
                st.session_state["destinos_liq"] = obtener_destinos_de_viaje(resultados[0]["id"])
            else:
                st.info(f"Encontré {len(resultados)} viajes que coinciden — elige el correcto:")
                for r in resultados:
                    rcol1, rcol2, rcol3, rcol4, rcol5 = st.columns([2, 2, 2, 2, 1])
                    rcol1.write(f"**{r['id_viaje']}**")
                    rcol2.write(r["cliente"])
                    rcol3.write(r["placa"])
                    rcol4.write(r["estado"])
                    if rcol5.button("Elegir", key=f"elegir_res_{r['id']}"):
                        st.session_state["viaje_liq"] = r
                        st.session_state["destinos_liq"] = obtener_destinos_de_viaje(r["id"])
                        st.rerun()
        elif "resultados_busqueda_liq" in st.session_state and not resultados and "viaje_liq" not in st.session_state:
            st.warning("No se encontró ningún viaje con ese dato.")

        viaje = st.session_state.get("viaje_liq")
        destinos = st.session_state.get("destinos_liq", [])

        if viaje:
            st.markdown("---")
            st.subheader(f"Viaje {viaje['id_viaje']}")
            i1, i2, i3, i4 = st.columns(4)
            i1.metric("Cliente", viaje["cliente"])
            i2.metric("Placa", viaje["placa"])
            i3.metric("Piloto", viaje["piloto"])
            i4.metric("Estado", viaje["estado"])

            if st.button("🖨️ Ver / Reimprimir Hoja de Control", key=f"hoja_{viaje['id']}"):
                components.html(generar_hoja_control_html(viaje, destinos), height=900, scrolling=True)

            if viaje["estado"] == "Liquidado":
                st.success(f"✅ Este viaje ya fue liquidado el {viaje['fecha_liquidacion']} "
                            f"{viaje['hora_liquidacion']} por {viaje['usuario_liquido']}.")
                st.caption("Para corregir algo o anularlo, ve a la pestaña **Gestión de Viajes**.")
            elif viaje["estado"] == "Anulado":
                st.error(f"🚫 Este viaje fue anulado el {viaje['fecha_anulacion']} por "
                         f"{viaje['usuario_anulo']}. Motivo: {viaje['motivo_anulacion']}")
            else:
                st.markdown("#### Devoluciones por tienda")
                destinos_actualizados = []
                for d in destinos:
                    st.markdown(f"📍 **{d['tienda']}** — marchamo ida: `{d['marchamo_ida']}`")
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        roles_dev = st.number_input(
                            "Roles devueltos", min_value=0, step=1, value=None, placeholder="0", key=f"roldev_{d['id']}"
                        ) or 0
                    with c2:
                        tarimas_dev = st.number_input(
                            "Tarimas devueltas", min_value=0, step=1, value=None, placeholder="0", key=f"tardev_{d['id']}"
                        ) or 0
                    with c3:
                        pacas_dev = st.number_input(
                            "Pacas de cartón devueltas", min_value=0, step=1, value=None, placeholder="0", key=f"pacdev_{d['id']}"
                        ) or 0
                    destinos_actualizados.append({
                        "id": d["id"],
                        "roles_devueltos": roles_dev,
                        "tarimas_devueltas": tarimas_dev,
                        "pacas_carton_devueltas": pacas_dev
                    })
                    st.markdown("---")

                if st.button(":material/check: Liquidar Viaje"):
                    ok, msg = liquidar_viaje(viaje["id"], destinos_actualizados, usuario_activo)
                    if ok:
                        st.success(f"Viaje {viaje['id_viaje']} liquidado correctamente. "
                                   "Ya puede procesarse el pago al transportista.")
                        del st.session_state["viaje_liq"]
                        del st.session_state["destinos_liq"]
                        st.session_state.pop("resultados_busqueda_liq", None)
                        st.rerun()
                    else:
                        st.error(f"❌ Error al liquidar: {msg}")
    else:
        st.info("Tu perfil no tiene permisos para liquidar viajes.")

# ==========================================
# MÓDULO 2B: GESTIÓN DE VIAJES — Editar (solo si Pendiente de Liquidar) y Anular
# (disponible mientras no esté ya Anulado). Separado de Liquidaciones a propósito.
# ==========================================
with tab5:
    if perfil_activo in ["Administrador", "Operador"]:
        st.header(":material/edit_document: Gestión de Viajes")
        if perfil_activo == "Operador":
            st.caption("Puedes corregir o anular únicamente los viajes que tú mismo creaste. "
                       "Un viaje Liquidado ya no se puede editar — solo anular.")
        else:
            st.caption("Corrige datos de un viaje ya guardado, o anúlalo. Un viaje Liquidado ya no se puede "
                       "editar — solo anular.")

        col_val_g, col_btn_g = st.columns([4, 1])
        with col_val_g:
            valor_busqueda_g = st.text_input(
                "Buscar por No. de Viaje, Marchamo de Ida o Placa", key="valor_gestion"
            )
        with col_btn_g:
            st.write("")
            buscar_g = st.button(":material/search: Buscar", use_container_width=True, key="btn_buscar_gestion")

        if buscar_g:
            if valor_busqueda_g.strip():
                st.session_state["resultados_gestion"] = buscar_viajes(valor_busqueda_g)
                st.session_state.pop("viaje_gestion", None)
                st.session_state.pop("destinos_gestion", None)
            else:
                st.warning("Escribe algo para buscar.")

        resultados_g = st.session_state.get("resultados_gestion", [])
        if resultados_g and "viaje_gestion" not in st.session_state:
            if len(resultados_g) == 1:
                st.session_state["viaje_gestion"] = resultados_g[0]
                st.session_state["destinos_gestion"] = obtener_destinos_de_viaje(resultados_g[0]["id"])
            else:
                st.info(f"Encontré {len(resultados_g)} viajes que coinciden — elige el correcto:")
                for r in resultados_g:
                    rcol1, rcol2, rcol3, rcol4, rcol5 = st.columns([2, 2, 2, 2, 1])
                    rcol1.write(f"**{r['id_viaje']}**")
                    rcol2.write(r["cliente"])
                    rcol3.write(r["placa"])
                    rcol4.write(r["estado"])
                    if rcol5.button("Elegir", key=f"elegir_gestion_{r['id']}"):
                        st.session_state["viaje_gestion"] = r
                        st.session_state["destinos_gestion"] = obtener_destinos_de_viaje(r["id"])
                        st.rerun()
        elif "resultados_gestion" in st.session_state and not resultados_g and "viaje_gestion" not in st.session_state:
            st.warning("No se encontró ningún viaje con ese dato.")

        viaje_g = st.session_state.get("viaje_gestion")
        destinos_g = st.session_state.get("destinos_gestion", [])

        if viaje_g:
            st.markdown("---")
            st.subheader(f"Viaje {viaje_g['id_viaje']}")
            g1, g2, g3, g4 = st.columns(4)
            g1.metric("Cliente", viaje_g["cliente"])
            g2.metric("Placa", viaje_g["placa"])
            g3.metric("Piloto", viaje_g["piloto"])
            g4.metric("Estado", viaje_g["estado"])

            if st.button("🖨️ Ver / Reimprimir Hoja de Control", key=f"hoja_gestion_{viaje_g['id']}"):
                components.html(generar_hoja_control_html(viaje_g, destinos_g), height=900, scrolling=True)

            es_propietario = (perfil_activo == "Administrador") or (viaje_g["usuario_creador"] == usuario_activo)
            if not es_propietario:
                st.warning("🚫 Este viaje lo creó otro usuario — como Operador, solo puedes editar o anular "
                           "los viajes que tú mismo generaste.")
            elif viaje_g["estado"] == "Anulado":
                st.error(f"🚫 Este viaje ya fue anulado el {viaje_g['fecha_anulacion']} por "
                         f"{viaje_g['usuario_anulo']}. Motivo: {viaje_g['motivo_anulacion']}. No hay más acciones disponibles.")
            else:
                if viaje_g["estado"] == "Pendiente de Liquidar":
                    with st.expander("✏️ Editar este viaje (corregir datos digitados)", expanded=True):
                        st.caption("No. de Viaje, Cliente, y quién/cuándo se creó NO se pueden cambiar.")
                        tiendas_cliente_g = st.session_state.catalogos["clientes"].get(viaje_g["cliente"], {})

                        placas_disp = list(st.session_state.catalogos["camiones"].keys())
                        placa_idx = placas_disp.index(viaje_g["placa"]) if viaje_g["placa"] in placas_disp else 0
                        placa_edit = st.selectbox("Placa", placas_disp, index=placa_idx, key=f"edit_placa_{viaje_g['id']}")
                        datos_cam = st.session_state.catalogos["camiones"].get(placa_edit, {})
                        transportista_edit = datos_cam.get("transportista", viaje_g["transportista"])

                        pilotos_disp = st.session_state.catalogos["pilotos"]
                        pil_idx = pilotos_disp.index(viaje_g["piloto"]) if viaje_g["piloto"] in pilotos_disp else 0
                        piloto_edit = st.selectbox("Piloto", pilotos_disp, index=pil_idx, key=f"edit_piloto_{viaje_g['id']}")

                        aux_disp = st.session_state.catalogos["auxiliares"]
                        aux_idx = aux_disp.index(viaje_g["auxiliar"]) if viaje_g["auxiliar"] in aux_disp else 0
                        auxiliar_edit = st.selectbox("Auxiliar", aux_disp, index=aux_idx, key=f"edit_aux_{viaje_g['id']}")

                        st.markdown("##### Datos por tienda")
                        destinos_editados = []
                        for d in sorted(destinos_g, key=lambda x: x["orden"]):
                            tiendas_opciones = list(tiendas_cliente_g.keys())
                            tienda_idx = tiendas_opciones.index(d["tienda"]) if d["tienda"] in tiendas_opciones else 0
                            tienda_e = st.selectbox("Tienda", tiendas_opciones, index=tienda_idx, key=f"etienda_{d['id']}")
                            e1, e2, e3 = st.columns(3)
                            with e1:
                                roles_e = st.number_input("Roles", min_value=0, step=1, value=d["roles"], key=f"eroles_{d['id']}")
                            with e2:
                                tarimas_e = st.number_input("Tarimas", min_value=0, step=1, value=d["tarimas"], key=f"etarimas_{d['id']}")
                            with e3:
                                cajas_e = st.number_input("Cajas", min_value=0, step=1, value=d["cajas"], key=f"ecajas_{d['id']}")
                            mida_e = st.text_input("Marchamo Ida", value=d["marchamo_ida"], key=f"emida_{d['id']}")
                            dc1, dc2, dc3, dc4 = st.columns(4)
                            with dc1:
                                remitos_e = st.text_input("Remisión", value=d["remitos"] or "", max_chars=10, key=f"eremitos_{d['id']}")
                            with dc2:
                                devolucion_e = st.text_input("Devolución", value=d["devolucion"] or "", max_chars=10, key=f"edevolucion_{d['id']}")
                            with dc3:
                                creditos_e = st.text_input("Créditos", value=d["creditos"] or "", max_chars=10, key=f"ecreditos_{d['id']}")
                            with dc4:
                                pg_e = st.number_input("Cartas Sol. P&G", min_value=0, step=1, value=d["pg_cajas"] or 0, key=f"epg_{d['id']}")
                            obs_e = st.text_area("Observaciones", value=d["incidencias"] or "", key=f"eobs_{d['id']}")
                            es_comp_e = st.checkbox("¿Es complemento?", value=d["es_complemento"], key=f"ecomp_{d['id']}")
                            opciones_pago = ["Local", "Departamental"]
                            idx_pago = opciones_pago.index(d["tipo_pago"]) if d["tipo_pago"] in opciones_pago else 0
                            tipo_pago_e = st.selectbox("Clasificación de Destino", opciones_pago, index=idx_pago, key=f"etipopago_{d['id']}")
                            destinos_editados.append({
                                "id": d["id"], "orden": d["orden"], "tienda": tienda_e,
                                "roles": roles_e, "tarimas": tarimas_e, "cajas": cajas_e,
                                "marchamo_ida": mida_e.strip(),
                                "remitos": remitos_e.strip(), "devolucion": devolucion_e.strip(),
                                "creditos": creditos_e.strip(), "pg_cajas": pg_e,
                                "incidencias": obs_e.strip(), "es_complemento": es_comp_e,
                                "tipo_pago": tipo_pago_e
                            })
                            st.markdown("---")

                        st.markdown("##### Cierre del Viaje")
                        marchamo_regreso_actual_g = next((d["marchamo_regreso"] for d in destinos_g if d["marchamo_regreso"]), "")
                        marchamo_regreso_edit = st.text_input(
                            "Marchamo de Regreso (único para todo el viaje — se valida en la última tienda)",
                            value=marchamo_regreso_actual_g, key=f"emreg_viaje_{viaje_g['id']}"
                        )

                        if st.button("💾 Guardar Correcciones", key=f"btn_editar_{viaje_g['id']}"):
                            if not marchamo_regreso_edit.strip():
                                st.error("❌ El Marchamo de Regreso es obligatorio.")
                            else:
                                ok, msg = editar_viaje(viaje_g["id"], placa_edit, transportista_edit, piloto_edit,
                                                       auxiliar_edit, destinos_editados, marchamo_regreso_edit.strip())
                                if ok:
                                    st.success(f"Viaje {viaje_g['id_viaje']} corregido.")
                                    del st.session_state["viaje_gestion"]
                                    del st.session_state["destinos_gestion"]
                                    st.session_state.pop("resultados_gestion", None)
                                    st.rerun()
                                else:
                                    st.error(f"❌ Error al corregir: {msg}")
                else:
                    st.info("Este viaje ya está Liquidado — no se puede editar, solo anular.")

                with st.expander("🚫 Anular este viaje"):
                    st.caption("Usa esto solo si el viaje se digitó por error, o si hay que revertirlo. Queda "
                               "registrado quién y cuándo lo anuló; los marchamos usados quedan libres para "
                               "digitarse en otro viaje.")
                    motivo_anulacion = st.text_input("Motivo de la anulación", key=f"motivo_anular_{viaje_g['id']}")
                    if st.button(":material/block: Confirmar Anulación", key=f"btn_anular_{viaje_g['id']}"):
                        if not motivo_anulacion.strip():
                            st.warning("Escribe el motivo antes de anular.")
                        else:
                            ok, msg = anular_viaje(viaje_g["id"], usuario_activo, motivo_anulacion.strip())
                            if ok:
                                st.success(f"Viaje {viaje_g['id_viaje']} anulado.")
                                del st.session_state["viaje_gestion"]
                                del st.session_state["destinos_gestion"]
                                st.session_state.pop("resultados_gestion", None)
                                st.rerun()
                            else:
                                st.error(f"❌ Error al anular: {msg}")
    else:
        st.info("Solo el perfil Administrador puede editar o anular viajes.")

# ==========================================
# MÓDULO 3: REPORTES (pendiente de construir)
# ==========================================
with tab3:
    reporte_sel = st.selectbox(
        "Reporte", ["Bitácora de Viajes", "Resumen de Liquidaciones", "Control de Retornable (próximamente)", "Cajas por Camión (próximamente)"]
    )

    if reporte_sel == "Bitácora de Viajes":
        st.subheader(":material/receipt_long: Bitácora de Viajes")

        fcol1, fcol2, fcol3, fcol4 = st.columns([1, 1, 1.3, 0.8])
        with fcol1:
            fecha_ini = st.date_input("Desde", value=ahora().date() - timedelta(days=7))
        with fcol2:
            fecha_fin = st.date_input("Hasta", value=ahora().date())
        with fcol3:
            clientes_reporte = ["Todos"] + clientes_permitidos_para(usuario_activo, perfil_activo)
            cliente_reporte = st.selectbox("Cliente", clientes_reporte)
        with fcol4:
            st.write("")
            generar = st.button(":material/search: Generar", use_container_width=True)

        if generar:
            st.session_state["df_bitacora"] = obtener_reporte_bitacora(fecha_ini, fecha_fin, cliente_reporte)

        df_bitacora = st.session_state.get("df_bitacora")
        if df_bitacora is not None:
            if df_bitacora.empty:
                st.info("No hay viajes en ese rango de fechas para ese cliente.")
            else:
                st.caption(f"{len(df_bitacora)} viaje(s) encontrados.")
                # Ventana con su propio scroll, en vez de empujar toda la página
                st.dataframe(df_bitacora, use_container_width=True, height=420)

                ecol1, ecol2 = st.columns(2)
                with ecol1:
                    st.download_button(
                        ":material/download: Exportar a Excel",
                        data=exportar_excel(df_bitacora),
                        file_name=f"bitacora_{fecha_ini}_a_{fecha_fin}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
                with ecol2:
                    st.download_button(
                        ":material/download: Exportar a CSV",
                        data=df_bitacora.to_csv(index=False).encode("utf-8-sig"),
                        file_name=f"bitacora_{fecha_ini}_a_{fecha_fin}.csv",
                        mime="text/csv",
                        use_container_width=True
                    )
        else:
            st.info("Elige el rango de fechas y el cliente, y presiona Generar.")

    elif reporte_sel == "Resumen de Liquidaciones":
        st.subheader(":material/fact_check: Resumen de Liquidaciones")

        lcol1, lcol2, lcol3, lcol4 = st.columns([1, 1, 1.3, 0.8])
        with lcol1:
            fecha_ini_l = st.date_input("Desde", value=ahora().date() - timedelta(days=7), key="fecha_ini_liq_rep")
        with lcol2:
            fecha_fin_l = st.date_input("Hasta", value=ahora().date(), key="fecha_fin_liq_rep")
        with lcol3:
            clientes_reporte_l = ["Todos"] + clientes_permitidos_para(usuario_activo, perfil_activo)
            cliente_reporte_l = st.selectbox("Cliente", clientes_reporte_l, key="cliente_liq_rep")
        with lcol4:
            st.write("")
            generar_l = st.button(":material/search: Generar", use_container_width=True, key="btn_generar_liq_rep")

        if generar_l:
            st.session_state["df_liquidaciones"] = obtener_reporte_liquidaciones(fecha_ini_l, fecha_fin_l, cliente_reporte_l)

        df_liq = st.session_state.get("df_liquidaciones")
        if df_liq is not None:
            if df_liq.empty:
                st.info("No hay viajes en ese rango de fechas para ese cliente.")
            else:
                total = len(df_liq)
                pendientes = (df_liq["Estado"] == "Pendiente de Liquidar").sum()
                liquidados = (df_liq["Estado"] == "Liquidado").sum()
                anulados = (df_liq["Estado"] == "Anulado").sum()

                kcol1, kcol2, kcol3, kcol4 = st.columns(4)
                kcol1.metric("Total de Viajes", total)
                kcol2.metric("Pendientes de Liquidar", pendientes)
                kcol3.metric("Liquidados", liquidados)
                kcol4.metric("Anulados", anulados)

                st.markdown("---")
                filtro_estado = st.selectbox("Filtrar por estado", ["Todos", "Pendiente de Liquidar", "Liquidado", "Anulado"], key="filtro_estado_liq_rep")
                df_mostrar = df_liq if filtro_estado == "Todos" else df_liq[df_liq["Estado"] == filtro_estado]
                st.dataframe(df_mostrar, use_container_width=True, height=380)

                ecol1, ecol2 = st.columns(2)
                with ecol1:
                    st.download_button(
                        ":material/download: Exportar a Excel",
                        data=exportar_excel(df_mostrar),
                        file_name=f"liquidaciones_{fecha_ini_l}_a_{fecha_fin_l}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True, key="excel_liq_rep"
                    )
                with ecol2:
                    st.download_button(
                        ":material/download: Exportar a CSV",
                        data=df_mostrar.to_csv(index=False).encode("utf-8-sig"),
                        file_name=f"liquidaciones_{fecha_ini_l}_a_{fecha_fin_l}.csv",
                        mime="text/csv",
                        use_container_width=True, key="csv_liq_rep"
                    )
        else:
            st.info("Elige el rango de fechas y el cliente, y presiona Generar.")
    else:
        st.info("Este reporte todavía no está construido — lo armamos en la próxima ronda.")

# ==========================================
# MÓDULO 4: CATÁLOGOS — descargar plantilla, llenar en Excel, subir para
# reemplazar el catálogo completo. Solo Administrador.
# ==========================================
with tab4:
    if perfil_activo != "Administrador":
        st.info("Solo el perfil Administrador puede gestionar catálogos.")
    else:
        st.header(":material/settings: Gestión de Catálogos")
        st.caption("Descarga la plantilla, llénala en Excel y súbela para reemplazar ese catálogo. "
                    "Los demás catálogos no se tocan.")

        catalogo_sel = st.selectbox("Catálogo a gestionar", list(CATALOGOS_CONFIG.keys()))
        config = CATALOGOS_CONFIG[catalogo_sel]
        columnas_mostrar = config["columnas"] + config.get("solo_lectura", [])
        df_actual_completo = leer_catalogo_actual(config["tabla"], columnas_mostrar)
        df_actual = df_actual_completo[config["columnas"]]  # sin las de solo lectura, para el resto de la lógica

        st.markdown("#### :material/table_edit: Edición rápida en tabla")
        st.caption("Edita directo aquí como en Excel — agrega filas al final o marca la casilla de la "
                   "izquierda para borrar una. Los cambios no se guardan solos: presiona 'Guardar Cambios'.")
        column_config = {c: st.column_config.Column(disabled=True) for c in config.get("solo_lectura", [])}
        df_editado = st.data_editor(
            df_actual_completo, use_container_width=True, height=280, num_rows="dynamic",
            column_config=column_config, key=f"editor_{catalogo_sel}"
        )
        if st.button(":material/save: Guardar Cambios de la Tabla", key=f"guardar_editor_{catalogo_sel}"):
            faltan = df_editado[config["clave"]].isnull().any(axis=1) | (df_editado[config["clave"]].astype(str).apply(lambda s: s.str.strip()).eq("").any(axis=1))
            if faltan.any():
                st.error(f"❌ Hay fila(s) sin llenar la llave ({', '.join(config['clave'])}). Complétalas o bórralas antes de guardar.")
            else:
                ok, msg = sincronizar_catalogo(config["tabla"], config["columnas"], config["clave"], df_editado[config["columnas"]])
                if ok:
                    st.success("✅ Tabla actualizada.")
                    if msg != "OK":
                        st.warning(msg)
                    st.session_state.catalogos = cargar_catalogos_desde_db()
                    st.rerun()
                else:
                    st.error(f"❌ Error: {msg}")
        st.caption(f"{len(df_actual)} registro(s) actualmente.")
        st.download_button(
            ":material/download: Descargar datos actuales (Excel)",
            data=exportar_excel(df_actual_completo),
            file_name=f"{config['tabla']}_actual.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"descargar_actual_{catalogo_sel}"
        )

        st.markdown("#### ➕ Agregar o corregir UN registro")
        st.caption("Para un cambio puntual, sin tener que subir un Excel completo. Si la llave "
                   "ya existe, se actualiza en vez de duplicarse.")

        NUEVO_CLIENTE_OPCION = "➕ Nuevo cliente..."
        valores_form = {}
        for col in config["columnas"]:
            if col == "cliente":
                # Selector en vez de texto libre: evita que cada persona escriba el
                # mismo cliente con variaciones distintas (typos, mayúsculas, espacios).
                clientes_existentes = sorted(st.session_state.catalogos["clientes_lista"])
                cliente_elegido = st.selectbox(
                    "Cliente", clientes_existentes + [NUEVO_CLIENTE_OPCION], key=f"campo_{catalogo_sel}_cliente_sel"
                )
                if cliente_elegido == NUEVO_CLIENTE_OPCION:
                    valores_form["cliente"] = st.text_input("Nombre del cliente nuevo", key=f"campo_{catalogo_sel}_cliente_nuevo")
                else:
                    valores_form["cliente"] = cliente_elegido
            elif col == "usuario" and catalogo_sel != "Usuarios":
                # En "Acceso Usuario → Cliente" el usuario debe ser uno que ya
                # exista en el catálogo de Usuarios, no texto libre.
                usuarios_existentes = sorted(st.session_state.catalogos["usuarios"].keys())
                valores_form["usuario"] = st.selectbox("Usuario", usuarios_existentes, key=f"campo_{catalogo_sel}_usuario_sel")
            elif col == "tipo" and catalogo_sel == "Rendimiento por Camión":
                # Los tonelajes válidos son los que ya existen en el catálogo de
                # Camiones — así se evitan variantes como "5 Ton", "5 T", "05 Ton".
                tipos_existentes = sorted(set(
                    c["tipo"] for c in st.session_state.catalogos["camiones"].values() if c["tipo"]
                ))
                if tipos_existentes:
                    valores_form["tipo"] = st.selectbox("Tipo", tipos_existentes, key=f"campo_{catalogo_sel}_tipo_sel")
                else:
                    st.warning("Todavía no hay ningún tonelaje registrado en el catálogo de Camiones.")
                    valores_form["tipo"] = ""
            elif col == "piloto" and catalogo_sel == "Camiones":
                pilotos_existentes = sorted(st.session_state.catalogos["pilotos"])
                valores_form["piloto"] = st.selectbox("Piloto", pilotos_existentes, key=f"campo_{catalogo_sel}_piloto_sel") if pilotos_existentes else ""
            elif col == "auxiliar" and catalogo_sel == "Camiones":
                auxiliares_existentes = sorted(st.session_state.catalogos["auxiliares"])
                valores_form["auxiliar"] = st.selectbox("Auxiliar", auxiliares_existentes, key=f"campo_{catalogo_sel}_auxiliar_sel") if auxiliares_existentes else ""
            elif col == "transportista" and catalogo_sel == "Camiones":
                transportistas_existentes = sorted(st.session_state.catalogos["transportistas"])
                valores_form["transportista"] = st.selectbox("Transportista", transportistas_existentes, key=f"campo_{catalogo_sel}_transportista_sel") if transportistas_existentes else ""
            elif col in config["numericas"]:
                valores_form[col] = st.number_input(col.replace("_", " ").title(), key=f"campo_{catalogo_sel}_{col}")
            else:
                valores_form[col] = st.text_input(col.replace("_", " ").title(), key=f"campo_{catalogo_sel}_{col}")
        guardar_registro = st.button(":material/save: Guardar Registro", key=f"btn_guardar_{catalogo_sel}")
        if guardar_registro:
            faltan_llave = [c for c in config["clave"] if not str(valores_form[c]).strip()]
            if faltan_llave:
                st.error(f"❌ Debes llenar: {', '.join(faltan_llave)} (son la llave del registro).")
            else:
                # Si el registro trae un cliente que todavía no existe en el catálogo
                # de Clientes, hay que crearlo primero — si no, la llave foránea lo rechaza.
                if "cliente" in valores_form and catalogo_sel != "Clientes":
                    agregar_o_actualizar_registro("cat_clientes", ["nombre"], ["nombre"], {"nombre": valores_form["cliente"]})
                ok, msg = agregar_o_actualizar_registro(config["tabla"], config["columnas"], config["clave"], valores_form)
                if ok:
                    st.success("✅ Registro guardado.")
                    st.session_state.catalogos = cargar_catalogos_desde_db()
                    st.rerun()
                else:
                    st.error(f"❌ Error: {msg}")

        if not df_actual.empty:
            st.markdown("#### 🗑️ Eliminar un registro")
            opciones_borrar = df_actual.apply(lambda r: " | ".join(str(r[c]) for c in config["clave"]), axis=1).tolist()
            registro_borrar = st.selectbox("Selecciona el registro a eliminar", opciones_borrar, key=f"del_sel_{catalogo_sel}")
            if st.button(":material/delete: Eliminar Registro Seleccionado", key=f"del_btn_{catalogo_sel}"):
                valores_clave = dict(zip(config["clave"], registro_borrar.split(" | ")))
                ok, msg = eliminar_registro(config["tabla"], config["clave"], valores_clave)
                if ok:
                    st.success("✅ Registro eliminado.")
                    st.session_state.catalogos = cargar_catalogos_desde_db()
                    st.rerun()
                else:
                    st.error(f"❌ Error: {msg}")

        st.markdown("---")
        st.markdown("#### 📤 Carga masiva (Excel)")
        col_desc, col_sub = st.columns(2)
        with col_desc:
            st.markdown("#### 1. Descargar plantilla")
            st.download_button(
                ":material/download: Descargar plantilla Excel",
                data=generar_plantilla_excel(config["columnas"]),
                file_name=f"plantilla_{config['tabla']}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

        with col_sub:
            st.markdown("#### 2. Subir y actualizar")
            archivo = st.file_uploader("Excel con los datos nuevos", type=["xlsx"], key=f"upload_{catalogo_sel}")

        if archivo is not None:
            try:
                df_nuevo = pd.read_excel(archivo, engine="openpyxl")
                faltantes = [c for c in config["columnas"] if c not in df_nuevo.columns]
                if faltantes:
                    st.error(f"❌ Al archivo le faltan estas columnas: {', '.join(faltantes)}")
                else:
                    st.markdown("#### Vista previa de lo que se va a cargar")
                    st.dataframe(df_nuevo[config["columnas"]], use_container_width=True)
                    st.warning(f"⚠️ Esto agrega/actualiza los registros del archivo, y borra los que ya no "
                               f"aparecen en él (salvo que estén en uso en Camiones o Viajes) — "
                               f"'{catalogo_sel}': {len(df_actual)} registro(s) actuales → {len(df_nuevo)} en el archivo.")
                    confirmar = st.checkbox("Confirmo este cambio", key=f"conf_{catalogo_sel}")
                    if st.button("🔄 Actualizar Catálogo", disabled=not confirmar):
                        ok, msg = sincronizar_catalogo(config["tabla"], config["columnas"], config["clave"], df_nuevo)
                        if ok:
                            st.success(f"✅ Catálogo '{catalogo_sel}' actualizado con {len(df_nuevo)} registro(s).")
                            if msg != "OK":
                                st.warning(msg)
                            st.session_state.catalogos = cargar_catalogos_desde_db()
                            st.rerun()
                        else:
                            st.error(f"❌ Error al actualizar: {msg}")
            except Exception as e:
                st.error(f"❌ No se pudo leer el archivo: {e}")
