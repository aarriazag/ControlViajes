import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import psycopg2
import psycopg2.extras
import json
import io
import os
import html as html_lib
import hashlib
import binascii
import secrets
import string
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import closing

# --- VERSIÓN DE LA APP ---
# Formato estándar Mayor.Menor.Parche:
#   Parche  (el último número) = cambios chiquitos/estéticos (ej. 1.4.2 → 1.4.3)
#   Menor   (el de en medio)   = funciones o reportes nuevos, sin romper nada existente (ej. 1.4.9 → 1.5.0)
#   Mayor   (el primero)       = cambio de fondo en cómo funciona la herramienta (ej. 1.9.4 → 2.0.0)
# Se actualiza a mano en cada entrega — no se calcula solo.
VERSION_APP = "1.0.0"

# Equivalencia acordada con el cliente: cada paca de cartón retornada equivale
# a 50 lbs — se usa para reportar el retornable de cartón en libras, que es
# como lo piden, en vez de solo en cantidad de pacas.
LBS_POR_PACA_CARTON = 50

# Configuración de la página web con estilo e identidad corporativa
st.set_page_config(page_title="Ransa | Control de Ruta", layout="wide", page_icon="🚚")

# El servidor (Render) corre en otro huso horario — todos los usuarios de esta
# app están en Guatemala, así que fijamos la hora ahí en vez de usar la hora
# del servidor o intentar leer la del navegador de cada quien.
ZONA_HORARIA = ZoneInfo("America/Guatemala")


def ahora():
    return datetime.now(ZONA_HORARIA)


# --- CONTRASEÑAS: nunca se guardan en texto plano. Se guarda una sal (salt)
# aleatoria por usuario + el resultado de aplicarle PBKDF2-SHA256 con muchas
# iteraciones a la contraseña — así, aunque alguien viera la base de datos, no
# puede recuperar la contraseña original. ---
ITERACIONES_HASH = 260_000


def hash_password(password, salt_hex=None):
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    hash_bytes = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERACIONES_HASH)
    return binascii.hexlify(salt).decode(), binascii.hexlify(hash_bytes).decode()


def password_coincide(password, salt_hex, hash_hex):
    if not salt_hex or not hash_hex:
        return False
    _, calculado = hash_password(password, salt_hex)
    return secrets.compare_digest(calculado, hash_hex)


def generar_password_temporal(longitud=10):
    alfabeto = string.ascii_letters + string.digits
    return "".join(secrets.choice(alfabeto) for _ in range(longitud))


def password_es_valida(password):
    """Mínimo 8 caracteres, al menos una letra y al menos un número — no es
    infalible, pero ya evita las más obvias ('12345678', 'contraseña')."""
    if len(password) < 8:
        return False, "Debe tener al menos 8 caracteres."
    if not any(c.isalpha() for c in password):
        return False, "Debe incluir al menos una letra."
    if not any(c.isdigit() for c in password):
        return False, "Debe incluir al menos un número."
    return True, "OK"


def registrar_auditoria(usuario, accion, detalle=""):
    """Deja rastro de quién hizo qué y cuándo. Es 'best effort': si por algo
    falla, no debe tumbar la operación que se estaba auditando — solo se
    registra el fallo en la consola del servidor."""
    try:
        with closing(get_conn()) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auditoria (fecha_hora, usuario, accion, detalle) VALUES (%s,%s,%s,%s)",
                (ahora().replace(tzinfo=None), usuario, accion, detalle)
            )
            conn.commit()
    except Exception as e:
        print(f"[auditoria] No se pudo registrar: {e}")


_MARCADOR_ERROR_TECNICO = "⚠️TECNICO⚠️"


def _error_tecnico(e, contexto=""):
    """Se usa DENTRO del except de las funciones que hablan con la base de
    datos. Nunca se le entrega el mensaje crudo de la excepción a nadie —
    pero SÍ se muestra, a cualquier rol, en qué parte de la app pasó y de qué
    tipo de error se trata (eso no expone nada sensible, y hace que una
    simple captura de pantalla ya traiga con qué empezar a revisar, sin
    depender de que alguien entre a Render). El texto completo de la
    excepción se manda siempre a la consola del servidor."""
    fecha_hora = ahora().strftime("%Y-%m-%d %H:%M:%S")
    tipo_error = type(e).__name__
    print(f"[ERROR {fecha_hora}] {contexto} ({tipo_error}): {e}")
    return f"{_MARCADOR_ERROR_TECNICO}{fecha_hora}||{contexto}||{tipo_error}::{e}"


@st.dialog("⚠️ Este viaje ya cambió")
def _dialog_conflicto_concurrencia(mensaje):
    """Ventana emergente para cuando dos personas trabajan el mismo viaje casi
    al mismo tiempo — es fácil que este aviso se pierda si solo aparece hasta
    abajo de una pantalla larga, así que aquí sí interrumpe de verdad."""
    st.warning(mensaje)
    st.caption("No es un error del sistema — solo significa que alguien más ya actuó sobre este viaje.")
    if st.button("Entendido, voy a revisar el viaje de nuevo", use_container_width=True):
        st.rerun()


def mostrar_resultado_error(msg, perfil_activo_usuario=None):
    """Muestra el resultado de una función de base de datos que falló.
    Si es un mensaje de negocio normal (ej. 'el marchamo ya existe'), se
    muestra tal cual — eso el usuario SÍ lo necesita leer completo.
    Si es un conflicto de dos personas editando el mismo viaje a la vez, se
    muestra como ventana emergente, para que no se pierda de vista.
    Si viene marcado como error técnico (_error_tecnico): a CUALQUIER rol se
    le muestra dónde pasó, de qué tipo fue, y cuándo — pensado para que una
    simple captura de pantalla, mandada por cualquier persona (no solo un
    Administrador viéndolo en vivo), ya sea suficiente para empezar a
    revisarlo sin depender de entrar a Render. El mensaje crudo de la
    excepción sigue sin mostrarse — eso solo lo ve un Administrador, en un
    desplegable aparte, por si acaso alguien con ese rol lo está viendo en
    el momento."""
    if isinstance(msg, str) and msg.startswith(_MARCADOR_ERROR_TECNICO):
        resto = msg[len(_MARCADOR_ERROR_TECNICO):]
        partes, _, detalle_completo = resto.partition("::")
        try:
            fecha_hora, contexto, tipo_error = partes.split("||")
        except ValueError:
            fecha_hora, contexto, tipo_error = partes, "?", "?"
        st.error(
            f"❌ Ocurrió un error al procesar la solicitud.\n\n"
            f"**Dónde:** `{contexto}`  \n**Tipo:** `{tipo_error}`  \n**Cuándo:** `{fecha_hora}`\n\n"
            f"Manda una captura de esto tal cual — con esta información ya se puede empezar a revisar."
        )
        if perfil_activo_usuario in ("Administrador", "SuperAdministrador"):
            with st.expander("🔧 Detalle técnico completo (solo Administrador)"):
                st.code(detalle_completo, language="text")
    elif isinstance(msg, str) and "alguien más" in msg:
        _dialog_conflicto_concurrencia(msg)
    else:
        st.error(f"❌ {msg}")

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
        /* Menos espacio vertical entre elementos del sidebar, para minimizar el
           scroll — sobre todo en pantallas anchas donde el sidebar es angosto
           pero alto. */
        section[data-testid="stSidebar"] div[data-testid="stVerticalBlock"] {
            gap: 0.35rem;
        }
        section[data-testid="stSidebar"] hr {
            margin: 0.35rem 0 !important;
        }
        section[data-testid="stSidebar"] .block-container {
            padding-top: 1.2rem;
            padding-bottom: 1rem;
        }
        section[data-testid="stSidebar"] div[data-testid="stCaptionContainer"] {
            margin-top: -0.3rem;
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
        .block-container { padding-top: 0.8rem; }
        /* Quita la barra de color ("decoración") que Streamlit pone arriba por
           defecto — es el espacio vacío/resaltado que sobra encima del contenido.
           El menú de los 3 puntos (⋮) se queda intacto, solo se quita esa franja. */
        div[data-testid="stDecoration"] { display: none; }
        header[data-testid="stHeader"] { height: 2.2rem; background: transparent; }

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

        /* "Chips" del Resumen de Datos: todos del mismo tamaño, con un toque 3D
           sutil (borde + sombra + resalte superior), sin llegar a verse pesado. */
        .resumen-chip {
            background: var(--gris-fondo);
            border: 0.5px solid var(--gris-borde);
            border-radius: 10px;
            padding: 8px 6px;
            text-align: center;
            box-shadow: 0 1px 2px rgba(0,0,0,0.08), inset 0 1px 0 rgba(255,255,255,0.6);
            height: 100%;
        }
        .resumen-chip-label { font-size: 11px; color: var(--gris-medio); }
        .resumen-chip-valor { font-size: 17px; font-weight: 600; color: var(--gris-texto); }
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


@st.cache_resource
def init_db():
    """Crea las tablas si no existen todavía. Con @st.cache_resource, Streamlit
    la ejecuta UNA sola vez por servidor (no en cada clic de cada usuario) —
    esto es lo que evita que dos sesiones intenten crear las mismas llaves
    foráneas al mismo tiempo y choquen entre sí (deadlock)."""
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
        cur.execute("ALTER TABLE cat_transportistas ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE")
        cur.execute("CREATE TABLE IF NOT EXISTS cat_pilotos (nombre TEXT PRIMARY KEY)")
        cur.execute("ALTER TABLE cat_pilotos ADD COLUMN IF NOT EXISTS codigo SERIAL")
        cur.execute("ALTER TABLE cat_pilotos ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE")
        cur.execute("CREATE TABLE IF NOT EXISTS cat_auxiliares (nombre TEXT PRIMARY KEY)")
        cur.execute("ALTER TABLE cat_auxiliares ADD COLUMN IF NOT EXISTS codigo SERIAL")
        cur.execute("ALTER TABLE cat_auxiliares ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cat_camiones (
                placa TEXT PRIMARY KEY, tipo TEXT, transportista TEXT, piloto TEXT, auxiliar TEXT
            )
        """)
        cur.execute("ALTER TABLE cat_camiones ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE")
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
        # Clasificación Local/Departamental: es un dato FIJO de la tienda (su
        # ubicación), no algo que se decida viaje a viaje — se configura aquí y
        # el Despacho solo la muestra, ya no se puede escoger ahí.
        cur.execute("ALTER TABLE cat_clientes_tiendas ADD COLUMN IF NOT EXISTS clasificacion TEXT DEFAULT 'Local'")
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
        cur.execute("ALTER TABLE cat_usuarios ADD COLUMN IF NOT EXISTS password_hash TEXT")
        cur.execute("ALTER TABLE cat_usuarios ADD COLUMN IF NOT EXISTS password_salt TEXT")
        cur.execute("ALTER TABLE cat_usuarios ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE")
        cur.execute("ALTER TABLE cat_usuarios ADD COLUMN IF NOT EXISTS debe_cambiar_password BOOLEAN DEFAULT TRUE")
        cur.execute("ALTER TABLE cat_usuarios ADD COLUMN IF NOT EXISTS creado_por TEXT")
        # Protección contra fuerza bruta: cuenta los intentos fallidos seguidos,
        # y bloquea la cuenta temporalmente si se pasa del límite.
        cur.execute("ALTER TABLE cat_usuarios ADD COLUMN IF NOT EXISTS intentos_fallidos INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE cat_usuarios ADD COLUMN IF NOT EXISTS bloqueado_hasta TIMESTAMP")
        # Qué clientes puede ver/trabajar cada usuario (excepto Administrador, que ve todos).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cat_usuario_clientes (
                usuario TEXT, cliente TEXT, PRIMARY KEY (usuario, cliente)
            )
        """)
        # Clientes como catálogo propio (antes solo existían "implícitos" dentro de
        # Clientes y Tiendas) — necesario para poder referenciarlos con llave foránea.
        cur.execute("CREATE TABLE IF NOT EXISTS cat_clientes (id SERIAL, nombre TEXT PRIMARY KEY)")
        cur.execute("ALTER TABLE cat_clientes ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE")
        # Bitácora de auditoría: quién hizo qué y cuándo, en toda la app.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auditoria (
                id SERIAL PRIMARY KEY,
                fecha_hora TIMESTAMP NOT NULL,
                usuario TEXT NOT NULL,
                accion TEXT NOT NULL,
                detalle TEXT
            )
        """)
        # Plan de carga por hora — la meta de camiones/bultos contra la que se
        # compara lo real en el Dashboard de indicadores (Estatus de Carga).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS plan_carga_horario (
                fecha DATE NOT NULL,
                hora INTEGER NOT NULL CHECK (hora >= 0 AND hora <= 23),
                camiones_plan INTEGER DEFAULT 0,
                bultos_plan INTEGER DEFAULT 0,
                PRIMARY KEY (fecha, hora)
            )
        """)
        conn.commit()

        # Sembrar datos de ejemplo — cada INSERT usa ON CONFLICT DO NOTHING, así
        # que es seguro que este bloque corra más de una vez (por ejemplo, si un
        # Borrado Masivo deja alguna de estas tablas en cero: eso no debe hacer
        # que se vuelvan a sembrar TODAS, chocando con lo que sí sigue ahí).
        cur.execute("SELECT COUNT(*) FROM cat_clientes_tiendas")
        if cur.fetchone()[0] == 0:
            cur.executemany("INSERT INTO cat_transportistas (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING",
                             [("Transportes Express",), ("Logística del Norte",), ("Flota Interna",)])
            cur.executemany("INSERT INTO cat_pilotos (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING",
                             [("Juan Pérez",), ("María Rodríguez",), ("Luis Martínez",), ("Andrés Custodio",)])
            cur.executemany("INSERT INTO cat_auxiliares (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING",
                             [("Carlos López",), ("Pedro Gómez",), ("José Hernández",), ("Ramiro Ruiz",)])
            cur.executemany(
                "INSERT INTO cat_camiones (placa, tipo, transportista, piloto, auxiliar) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (placa) DO NOTHING",
                [("C-123ABC", "5 Ton", "Transportes Express", "Juan Pérez", "Carlos López"),
                 ("C-456DEF", "10 Ton", "Logística del Norte", "María Rodríguez", "Pedro Gómez"),
                 ("C-789GHI", "20 Ton", "Flota Interna", "Luis Martínez", "José Hernández")]
            )
            cur.executemany(
                "INSERT INTO cat_clientes (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING",
                [("Dollarcity",), ("UniSuper",), ("UniSuper Importados",), ("UniSuper LTX",)]
            )
            cur.executemany(
                "INSERT INTO cat_clientes_tiendas (cliente, tienda, km) VALUES (%s,%s,%s) "
                "ON CONFLICT (cliente, tienda) DO NOTHING",
                [("Dollarcity", "Dollarcity Zona 10", 15.5),
                 ("Dollarcity", "Dollarcity Mixco", 32.0),
                 ("UniSuper", "UniSuper Central", 22.1),
                 ("UniSuper Importados", "UniSuper Importados Norte", 18.0),
                 ("UniSuper LTX", "UniSuper LTX Sur", 45.3)]
            )
            cur.executemany(
                "INSERT INTO cat_rendimiento_camion (tipo, km_por_galon) VALUES (%s,%s) ON CONFLICT (tipo) DO NOTHING",
                [("5 Ton", 8.0), ("10 Ton", 6.0), ("20 Ton", 4.0)]
            )
            cur.executemany(
                "INSERT INTO cat_cds_por_cliente (cliente, cd) VALUES (%s,%s) ON CONFLICT (cliente, cd) DO NOTHING",
                [("Dollarcity", "CD Barcenas"), ("Dollarcity", "CD Central"),
                 ("UniSuper", "CD Barcenas"),
                 ("UniSuper Importados", "CD Barcenas"),
                 ("UniSuper LTX", "CD Barcenas")]
            )
            cur.executemany(
                "INSERT INTO cat_usuarios (usuario, perfil) VALUES (%s,%s) ON CONFLICT (usuario) DO NOTHING",
                [("Admin_Logistica", "SuperAdministrador"), ("Op_Salidas", "Operador"), ("Liq_Transporte", "Liquidador")]
            )
            # Por defecto, los usuarios de ejemplo (no-Administrador) ven todos los
            # clientes sembrados, para no romper nada mientras ajustas los accesos reales.
            cur.executemany(
                "INSERT INTO cat_usuario_clientes (usuario, cliente) VALUES (%s,%s) ON CONFLICT (usuario, cliente) DO NOTHING",
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

        # Contraseña temporal para cualquier usuario que todavía no tenga una
        # (los 3 usuarios de ejemplo, o upgrades desde una versión sin login real).
        # Todos quedan forzados a cambiarla en su primer ingreso.
        cur.execute("SELECT usuario FROM cat_usuarios WHERE password_hash IS NULL")
        usuarios_sin_password = [r[0] for r in cur.fetchall()]
        if usuarios_sin_password:
            salt_temp, hash_temp = hash_password("Ransa2026")
            for u in usuarios_sin_password:
                cur.execute(
                    "UPDATE cat_usuarios SET password_hash=%s, password_salt=%s, debe_cambiar_password=TRUE WHERE usuario=%s",
                    (hash_temp, salt_temp, u)
                )
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
        cur.execute("SELECT usuario, perfil FROM cat_usuarios ORDER BY usuario")
        usuarios = {r["usuario"]: r["perfil"] for r in cur.fetchall()}

        cur.execute("SELECT nombre FROM cat_transportistas WHERE activo = TRUE ORDER BY nombre")
        transportistas = [r["nombre"] for r in cur.fetchall()]

        cur.execute("SELECT nombre FROM cat_pilotos WHERE activo = TRUE ORDER BY nombre")
        pilotos = [r["nombre"] for r in cur.fetchall()]

        cur.execute("SELECT nombre FROM cat_auxiliares WHERE activo = TRUE ORDER BY nombre")
        auxiliares = [r["nombre"] for r in cur.fetchall()]

        cur.execute("SELECT placa, tipo, transportista, piloto, auxiliar FROM cat_camiones WHERE activo = TRUE ORDER BY placa")
        camiones = {r["placa"]: {"tipo": r["tipo"], "transportista": r["transportista"],
                                  "piloto": r["piloto"], "auxiliar": r["auxiliar"]} for r in cur.fetchall()}

        cur.execute("SELECT cliente, tienda, km, clasificacion FROM cat_clientes_tiendas ORDER BY cliente, tienda")
        clientes = {}
        for r in cur.fetchall():
            clientes.setdefault(r["cliente"], {})[r["tienda"]] = {"km": r["km"], "clasificacion": r["clasificacion"] or "Local"}

        cur.execute("SELECT tipo, km_por_galon FROM cat_rendimiento_camion ORDER BY tipo")
        rendimiento = {r["tipo"]: r["km_por_galon"] for r in cur.fetchall()}

        cur.execute("SELECT cliente, cd FROM cat_cds_por_cliente ORDER BY cliente, cd")
        cds_por_cliente = {}
        for r in cur.fetchall():
            cds_por_cliente.setdefault(r["cliente"], []).append(r["cd"])

        cur.execute("SELECT usuario, cliente FROM cat_usuario_clientes ORDER BY usuario, cliente")
        usuario_clientes = {}
        for r in cur.fetchall():
            usuario_clientes.setdefault(r["usuario"], []).append(r["cliente"])

        cur.execute("SELECT nombre FROM cat_clientes ORDER BY nombre")
        clientes_lista = [r["nombre"] for r in cur.fetchall()]
        # Aparte, solo los activos — para los menús donde se ASIGNA algo nuevo
        # (Despacho, dar acceso a un usuario, agregar una tienda). Los Reportes
        # y clientes_permitidos_para() siguen usando clientes_lista completa,
        # para no perder la posibilidad de consultar el historial de un
        # cliente que ya se desactivó.
        cur.execute("SELECT nombre FROM cat_clientes WHERE activo = TRUE ORDER BY nombre")
        clientes_lista_activos = [r["nombre"] for r in cur.fetchall()]

        return {
            "usuarios": usuarios,
            "transportistas": transportistas,
            "pilotos": pilotos,
            "auxiliares": auxiliares,
            "camiones": camiones,
            "clientes": clientes,
            "clientes_lista": clientes_lista,
            "clientes_lista_activos": clientes_lista_activos,
            "rendimiento": rendimiento,
            "cds_por_cliente": cds_por_cliente,
            "usuario_clientes": usuario_clientes
        }


def clientes_permitidos_para(usuario, perfil):
    """Administrador ve todos los clientes; cualquier otro perfil solo ve los
    clientes que tenga asignados en el catálogo de accesos.
    OJO: 'todos' se saca de la lista real de clientes (cat_clientes), NO de
    qué clientes tienen tiendas cargadas — si un cliente se queda sin ninguna
    tienda (por ejemplo, tras un Borrado Masivo), sigue siendo un cliente
    válido y la gente no debería perder el acceso a él por eso."""
    todos = st.session_state.catalogos["clientes_lista"]
    if perfil in ("Administrador", "SuperAdministrador"):
        return todos
    return [c for c in st.session_state.catalogos["usuario_clientes"].get(usuario, []) if c in todos]


def cuenta_sigue_activa(usuario):
    """Revisa el estado 'activo' de la cuenta contra la base de datos — se usa
    en CADA acción (no solo al iniciar sesión), para que desactivar a alguien
    lo saque de la app de inmediato, aunque ya tuviera una sesión abierta."""
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT activo FROM cat_usuarios WHERE usuario = %s", (usuario,))
        row = cur.fetchone()
        return bool(row and row[0])


def _credenciales_validas(usuario, password):
    """Chequeo puro de usuario+contraseña, SIN dejar rastro en la auditoría —
    lo usa verificar_login() (que sí audita) y el cambio de contraseña propia
    (que no debe registrarse como si fuera un intento de inicio de sesión)."""
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT usuario, perfil, password_hash, password_salt, activo, debe_cambiar_password "
            "FROM cat_usuarios WHERE usuario = %s", (usuario,)
        )
        row = cur.fetchone()
        if not row or not row["activo"] or not password_coincide(password, row["password_salt"], row["password_hash"]):
            return None
        return {"perfil": row["perfil"], "debe_cambiar_password": row["debe_cambiar_password"]}


MAX_INTENTOS_LOGIN = 5
MINUTOS_BLOQUEO_LOGIN = 15


def verificar_login(usuario, password):
    """Como _credenciales_validas(), pero además:
    - deja rastro en la auditoría (éxitos y fallos)
    - bloquea la cuenta 15 minutos tras 5 intentos fallidos seguidos, para que
      alguien no pueda probar contraseñas sin límite (fuerza bruta).
    Úsalo solo en la pantalla de inicio de sesión — el cambio de contraseña
    propia sigue usando _credenciales_validas() directo, sin este conteo."""
    ahora_sin_tz = ahora().replace(tzinfo=None)
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT usuario, perfil, password_hash, password_salt, activo, debe_cambiar_password, "
            "intentos_fallidos, bloqueado_hasta FROM cat_usuarios WHERE usuario = %s", (usuario,)
        )
        row = cur.fetchone()

        if row and row["bloqueado_hasta"] and row["bloqueado_hasta"] > ahora_sin_tz:
            registrar_auditoria(usuario, "Intento de login FALLIDO (cuenta bloqueada temporalmente)")
            return None

        if row and row["activo"] and password_coincide(password, row["password_salt"], row["password_hash"]):
            cur.execute("UPDATE cat_usuarios SET intentos_fallidos=0, bloqueado_hasta=NULL WHERE usuario=%s", (usuario,))
            conn.commit()
            registrar_auditoria(usuario, "Login exitoso")
            return {"perfil": row["perfil"], "debe_cambiar_password": row["debe_cambiar_password"]}

        if row:
            nuevos_intentos = (row["intentos_fallidos"] or 0) + 1
            if nuevos_intentos >= MAX_INTENTOS_LOGIN:
                cur.execute(
                    "UPDATE cat_usuarios SET intentos_fallidos=0, bloqueado_hasta=%s WHERE usuario=%s",
                    (ahora_sin_tz + timedelta(minutes=MINUTOS_BLOQUEO_LOGIN), usuario)
                )
                conn.commit()
                registrar_auditoria(usuario, "Cuenta bloqueada temporalmente por intentos fallidos",
                                     f"{MAX_INTENTOS_LOGIN} intentos seguidos — bloqueada {MINUTOS_BLOQUEO_LOGIN} min")
            else:
                cur.execute("UPDATE cat_usuarios SET intentos_fallidos=%s WHERE usuario=%s", (nuevos_intentos, usuario))
                conn.commit()

    registrar_auditoria(usuario or "(vacío)", "Intento de login FALLIDO")
    return None


def establecer_password(usuario, password_nueva, forzar_cambio_siguiente=False, cambiado_por=None):
    salt, hashed = hash_password(password_nueva)
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE cat_usuarios SET password_hash=%s, password_salt=%s, debe_cambiar_password=%s WHERE usuario=%s",
                    (hashed, salt, forzar_cambio_siguiente, usuario)
                )
            conn.commit()
            quien = cambiado_por or usuario
            accion = "Cambiar su propia contraseña" if quien == usuario else "Restablecer contraseña de otro usuario"
            registrar_auditoria(quien, accion, f"Usuario afectado: {usuario}")
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, _error_tecnico(e, "establecer_password")


def crear_usuario(usuario, perfil, creado_por, clientes=None):
    """Crea un usuario nuevo con una contraseña temporal generada al azar
    (se le muestra una sola vez a quien lo crea, para que se la pase a la persona).
    Queda forzado a cambiarla en su primer ingreso.
    `clientes`: lista de clientes a los que se le da acceso EN EL MISMO PASO —
    así no puede quedar un usuario creado sin ningún cliente asignado, que es
    justo lo que lo deja atorado en 'no tienes ningún cliente asignado'."""
    password_temp = generar_password_temporal()
    salt, hashed = hash_password(password_temp)
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cat_usuarios (usuario, perfil, password_hash, password_salt, activo, "
                    "debe_cambiar_password, creado_por) VALUES (%s,%s,%s,%s,TRUE,TRUE,%s)",
                    (usuario, perfil, hashed, salt, creado_por)
                )
                for cliente in (clientes or []):
                    cur.execute(
                        "INSERT INTO cat_usuario_clientes (usuario, cliente) VALUES (%s,%s) "
                        "ON CONFLICT (usuario, cliente) DO NOTHING",
                        (usuario, cliente)
                    )
            conn.commit()
            registrar_auditoria(creado_por, "Crear usuario", f"Usuario nuevo: {usuario} · Perfil: {perfil} · Clientes: {', '.join(clientes or []) or '(ninguno)'}")
            return True, password_temp
        except Exception as e:
            conn.rollback()
            return False, _error_tecnico(e, "crear_usuario")


def cambiar_estado_usuario(usuario, activo, quien_cambia):
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE cat_usuarios SET activo=%s WHERE usuario=%s", (activo, usuario))
            conn.commit()
            registrar_auditoria(quien_cambia, "Activar/Desactivar usuario",
                                 f"Usuario: {usuario} · Nuevo estado: {'Activo' if activo else 'Desactivado'}")
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, _error_tecnico(e, "cambiar_estado_usuario")


def cambiar_perfil_usuario(usuario, perfil_nuevo, quien_cambia):
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE cat_usuarios SET perfil=%s WHERE usuario=%s", (perfil_nuevo, usuario))
            conn.commit()
            registrar_auditoria(quien_cambia, "Cambiar perfil de usuario", f"Usuario: {usuario} · Nuevo perfil: {perfil_nuevo}")
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, _error_tecnico(e, "cambiar_perfil_usuario")


def listar_usuarios_gestion(perfiles=None):
    """Lista de usuarios para la pantalla de Gestión de Usuarios. Si se pasa
    `perfiles`, solo devuelve usuarios con esos perfiles (para que un Supervisor
    no vea ni pueda tocar cuentas de Administrador u otro Supervisor)."""
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if perfiles:
            cur.execute(
                "SELECT usuario, perfil, activo, debe_cambiar_password, creado_por FROM cat_usuarios "
                "WHERE perfil = ANY(%s) ORDER BY usuario", (perfiles,)
            )
        else:
            cur.execute(
                "SELECT usuario, perfil, activo, debe_cambiar_password, creado_por FROM cat_usuarios ORDER BY usuario"
            )
        return cur.fetchall()


def obtener_auditoria(fecha_inicio, fecha_fin, usuario_filtro="Todos", limite=500):
    with closing(get_conn()) as conn:
        query = """
            SELECT fecha_hora AS "Fecha y Hora", usuario AS "Usuario", accion AS "Acción", detalle AS "Detalle"
            FROM auditoria
            WHERE fecha_hora::date BETWEEN %s AND %s
              AND (%s = 'Todos' OR usuario = %s)
            ORDER BY fecha_hora DESC
            LIMIT %s
        """
        return pd.read_sql_query(query, conn, params=(str(fecha_inicio), str(fecha_fin), usuario_filtro, usuario_filtro, limite))


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
    "Clientes": {"tabla": "cat_clientes", "columnas": ["nombre", "activo"], "clave": ["nombre"], "numericas": [],
                "booleanas": ["activo"], "solo_lectura": ["id"]},
    "Transportistas": {"tabla": "cat_transportistas", "columnas": ["nombre", "razon_social", "activo"], "clave": ["nombre"],
                       "numericas": [], "booleanas": ["activo"], "solo_lectura": ["codigo"]},
    "Pilotos": {"tabla": "cat_pilotos", "columnas": ["nombre", "activo"], "clave": ["nombre"], "numericas": [],
               "booleanas": ["activo"], "solo_lectura": ["codigo"]},
    "Auxiliares": {"tabla": "cat_auxiliares", "columnas": ["nombre", "activo"], "clave": ["nombre"], "numericas": [],
                  "booleanas": ["activo"], "solo_lectura": ["codigo"]},
    "Camiones": {"tabla": "cat_camiones", "columnas": ["placa", "tipo", "transportista", "piloto", "auxiliar", "activo"],
                 "clave": ["placa"], "numericas": [], "booleanas": ["activo"]},
    "Clientes y Tiendas": {"tabla": "cat_clientes_tiendas", "columnas": ["cliente", "tienda", "codigo_tienda", "km", "clasificacion"],
                           "clave": ["cliente", "tienda"], "numericas": ["codigo_tienda", "km"]},
    "Rendimiento por Camión": {"tabla": "cat_rendimiento_camion", "columnas": ["tipo", "km_por_galon"],
                               "clave": ["tipo"], "numericas": ["km_por_galon"]},
    "CDs por Cliente": {"tabla": "cat_cds_por_cliente", "columnas": ["cliente", "cd"],
                        "clave": ["cliente", "cd"], "numericas": []},
    # "Usuarios" ya no se gestiona aquí como catálogo genérico — crear una cuenta
    # necesita generarle una contraseña, así que vive en la pestaña "Usuarios"
    # dedicada (Gestión de Usuarios), no en un data_editor de texto plano.
    "Acceso Usuario → Cliente": {"tabla": "cat_usuario_clientes", "columnas": ["usuario", "cliente"],
                                 "clave": ["usuario", "cliente"], "numericas": []},
}

# Lista blanca de tablas/columnas para armar SQL dinámico — capa extra de
# defensa. Hoy el nombre de tabla/columna SIEMPRE sale de este mismo
# diccionario (nunca de lo que escribe un usuario, siempre de un menú fijo),
# pero si algún cambio futuro rompiera esa garantía sin que nadie se diera
# cuenta, esta validación lo detiene antes de que llegue a la base de datos.
TABLAS_PERMITIDAS = {cfg["tabla"] for cfg in CATALOGOS_CONFIG.values()}
COLUMNAS_PERMITIDAS_POR_TABLA = {
    cfg["tabla"]: set(cfg["columnas"]) | set(cfg.get("solo_lectura", []))
    for cfg in CATALOGOS_CONFIG.values()
}


def _validar_identificadores_sql(tabla, columnas=None):
    """Verifica que 'tabla' y cada nombre en 'columnas' estén en la lista
    blanca antes de usarlos para armar una consulta SQL dinámica. Lanza
    ValueError si algo no está permitido — nunca deja pasar un nombre que
    no reconozca."""
    if tabla not in TABLAS_PERMITIDAS:
        raise ValueError(f"Tabla no permitida: {tabla!r}")
    if columnas:
        permitidas = COLUMNAS_PERMITIDAS_POR_TABLA.get(tabla, set())
        for c in columnas:
            if c not in permitidas:
                raise ValueError(f"Columna no permitida en {tabla!r}: {c!r}")


def agregar_o_actualizar_registro(tabla, columnas, clave, valores, usuario, clientes_permitidos=None):
    """Inserta un registro nuevo, o lo actualiza si la llave ya existe (upsert),
    para poder corregir un solo dato sin tener que resubir todo el Excel."""
    if clientes_permitidos is not None and "cliente" in columnas:
        if str(valores.get("cliente", "")).strip() not in clientes_permitidos:
            return False, f"No tienes acceso al cliente '{valores.get('cliente')}'."
    with closing(get_conn()) as conn:
        try:
            _validar_identificadores_sql(tabla, columnas)
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
            registrar_auditoria(usuario, "Agregar/Actualizar registro de catálogo",
                                 f"Tabla {tabla} · {', '.join(f'{c}={valores[c]}' for c in clave)}")
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, _error_tecnico(e, "agregar_o_actualizar_registro")


def verificar_uso(tabla, valores_clave):
    """Revisa en qué otras tablas se usa este registro, ANTES de intentar
    borrarlo — para poder explicarle a la persona qué lo está bloqueando
    (ej. '47 viajes'), en vez de solo decirle 'no se pudo'. Es de solo
    lectura, no cambia nada."""
    mapa_referencias = {
        "cat_pilotos": [("cat_camiones", "piloto"), ("viajes", "piloto")],
        "cat_auxiliares": [("cat_camiones", "auxiliar"), ("viajes", "auxiliar")],
        "cat_transportistas": [("cat_camiones", "transportista"), ("viajes", "transportista")],
        "cat_clientes": [("cat_clientes_tiendas", "cliente"), ("cat_cds_por_cliente", "cliente"),
                         ("cat_usuario_clientes", "cliente"), ("viajes", "cliente")],
        "cat_camiones": [("viajes", "placa")],
    }
    referencias = mapa_referencias.get(tabla, [])
    if not referencias:
        return []
    valor = list(valores_clave.values())[0]
    resultados = []
    with closing(get_conn()) as conn, conn.cursor() as cur:
        for tabla_dep, columna_dep in referencias:
            cur.execute(f"SELECT COUNT(*) FROM {tabla_dep} WHERE {columna_dep} = %s", (valor,))
            cantidad = cur.fetchone()[0]
            if cantidad > 0:
                resultados.append((tabla_dep, cantidad))
    return resultados


def eliminar_registro(tabla, clave, valores_clave, usuario, clientes_permitidos=None):
    if clientes_permitidos is not None and "cliente" in clave:
        if str(valores_clave.get("cliente", "")).strip() not in clientes_permitidos:
            return False, f"No tienes acceso al cliente '{valores_clave.get('cliente')}'."
    with closing(get_conn()) as conn:
        try:
            _validar_identificadores_sql(tabla, clave)
            with conn.cursor() as cur:
                where_sql = " AND ".join(f"{c} = %s" for c in clave)
                cur.execute(f"DELETE FROM {tabla} WHERE {where_sql}", tuple(valores_clave[c] for c in clave))
            conn.commit()
            registrar_auditoria(usuario, "Eliminar registro de catálogo",
                                 f"Tabla {tabla} · {', '.join(f'{c}={valores_clave[c]}' for c in clave)}")
            return True, "OK"
        except psycopg2.errors.ForeignKeyViolation:
            conn.rollback()
            return False, ("No se puede borrar — todavía está en uso en otra parte del sistema (un camión, "
                            "un viaje, u otro catálogo lo está referenciando). Usa '🔍 Ver qué está usando esto' "
                            "para saber exactamente qué lo bloquea.")
        except Exception as e:
            conn.rollback()
            return False, _error_tecnico(e, "eliminar_registro")


def generar_plantilla_excel(columnas):
    """Genera un Excel vacío (solo encabezados) con las columnas correctas,
    para que el usuario lo llene y lo vuelva a subir."""
    buffer = io.BytesIO()
    pd.DataFrame(columns=columnas).to_excel(buffer, index=False, engine="openpyxl")
    return buffer.getvalue()


def leer_catalogo_actual(tabla, columnas):
    _validar_identificadores_sql(tabla, columnas)
    with closing(get_conn()) as conn:
        return pd.read_sql_query(f"SELECT {', '.join(columnas)} FROM {tabla}", conn)


def sincronizar_catalogo(tabla, columnas, clave, df_nuevo, usuario, clientes_permitidos=None, permitir_borrado=False):
    """Agrega/actualiza (upsert) los registros del DataFrame. Por defecto NUNCA
    borra nada — así quedó la carga masiva por Excel, para que un archivo
    incompleto jamás pueda perder datos sin que nadie se dé cuenta.

    `permitir_borrado=True` es solo para la tabla editable en pantalla, donde
    la persona ve la fila que está quitando antes de guardar — ahí sí es un
    borrado deliberado y visible. Aun con permitir_borrado=True, el borrado
    se limita a los clientes que aparecen en `df_nuevo` (nunca a todo lo que
    el usuario podría ver), y respeta las llaves foráneas: si algo está en
    uso en Camiones o Viajes, Postgres bloquea ESE borrado puntual y se sigue
    con el resto, avisando al final qué no se pudo quitar."""
    with closing(get_conn()) as conn:
        try:
            _validar_identificadores_sql(tabla, columnas)
            with conn.cursor() as cur:
                if clientes_permitidos is not None and "cliente" in columnas:
                    fuera_de_alcance = {str(row["cliente"]).strip() for _, row in df_nuevo.iterrows()
                                        if str(row["cliente"]).strip() not in clientes_permitidos}
                    if fuera_de_alcance:
                        return False, f"No tienes acceso a estos clientes: {', '.join(fuera_de_alcance)}"

                if "cliente" in columnas and tabla != "cat_clientes":
                    clientes_del_archivo = {str(row["cliente"]).strip() for _, row in df_nuevo.iterrows() if str(row["cliente"]).strip()}
                    for c in clientes_del_archivo:
                        cur.execute("INSERT INTO cat_clientes (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING", (c,))

                no_borrables = []
                if permitir_borrado:
                    # El borrado se limita a los clientes que aparecen EN el
                    # dataframe que se está guardando — nunca a todo lo que el
                    # usuario podría ver — así una tabla filtrada a un cliente
                    # jamás borra datos de otro cliente que ni siquiera se veía.
                    if "cliente" in columnas:
                        clientes_en_pantalla = sorted({str(row["cliente"]).strip() for _, row in df_nuevo.iterrows() if str(row["cliente"]).strip()})
                        if clientes_permitidos is not None:
                            clientes_en_pantalla = [c for c in clientes_en_pantalla if c in clientes_permitidos]
                        cur.execute(f"SELECT {', '.join(clave)} FROM {tabla} WHERE cliente = ANY(%s)", (clientes_en_pantalla,))
                    else:
                        cur.execute(f"SELECT {', '.join(clave)} FROM {tabla}")
                    claves_actuales = {tuple(str(v) for v in fila) for fila in cur.fetchall()}
                    claves_nuevas = {tuple(str(row[c]).strip() for c in clave) for _, row in df_nuevo.iterrows()}
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
            registrar_auditoria(usuario, "Sincronizar catálogo (Excel/tabla)", f"Tabla {tabla} · {len(df_nuevo)} fila(s)")
            if no_borrables:
                return True, f"⚠️ Guardado, pero esto sigue existiendo porque está en uso en Camiones o Viajes: {', '.join(no_borrables)}"
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, _error_tecnico(e, "sincronizar_catalogo")


def contar_borrado_masivo(tabla, columna_cliente, cliente_valor):
    """Solo lectura: cuántas filas coinciden con lo que se va a borrar — para
    mostrarlo ANTES de que la persona confirme nada."""
    _validar_identificadores_sql(tabla, [columna_cliente] if columna_cliente else None)
    with closing(get_conn()) as conn, conn.cursor() as cur:
        if columna_cliente is None or cliente_valor == "TODO EL CATÁLOGO":
            cur.execute(f"SELECT COUNT(*) FROM {tabla}")
        else:
            cur.execute(f"SELECT COUNT(*) FROM {tabla} WHERE {columna_cliente} = %s", (cliente_valor,))
        return cur.fetchone()[0]


def borrar_masivo(tabla, columna_cliente, cliente_valor, usuario):
    """Borrado masivo de verdad — solo para SuperAdministrador, y solo tras
    confirmación explícita en la pantalla. Si algo está en uso en otra tabla
    (Camiones, Viajes) y bloquea el DELETE por llave foránea, no se aplica
    ningún borrado parcial silencioso: se avisa con un mensaje claro."""
    with closing(get_conn()) as conn:
        try:
            _validar_identificadores_sql(tabla, [columna_cliente] if columna_cliente else None)
            with conn.cursor() as cur:
                if columna_cliente is None or cliente_valor == "TODO EL CATÁLOGO":
                    cur.execute(f"DELETE FROM {tabla}")
                else:
                    cur.execute(f"DELETE FROM {tabla} WHERE {columna_cliente} = %s", (cliente_valor,))
                borrados = cur.rowcount
            conn.commit()
            registrar_auditoria(usuario, "BORRADO MASIVO", f"Tabla {tabla} · alcance: {cliente_valor} · {borrados} fila(s) borradas")
            return True, borrados
        except psycopg2.errors.ForeignKeyViolation:
            conn.rollback()
            return False, ("No se pudo borrar nada — al menos un registro de este alcance todavía está en uso "
                            "en otra tabla (Camiones, Viajes, u otro catálogo). No se aplicó ningún borrado parcial.")
        except Exception as e:
            conn.rollback()
            return False, _error_tecnico(e, "borrar_masivo")


init_db()


def marchamo_ya_usado(marchamo, cur):
    """Un marchamo no se debe repetir sin importar si ya se usó como Marchamo de
    Ida o de Regreso en cualquier otro destino — es el mismo sello físico."""
    cur.execute("SELECT 1 FROM destinos WHERE marchamo_ida = %s OR marchamo_regreso = %s", (marchamo, marchamo))
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
                marchamo_regreso_viaje = destinos_viaje[-1]["marchamo_regreso"] if destinos_viaje else ""
                if marchamo_regreso_viaje and marchamo_ya_usado(marchamo_regreso_viaje, cur):
                    conn.rollback()
                    return False, f"El marchamo de regreso '{marchamo_regreso_viaje}' ya fue usado en otro viaje."

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
            orden_tiendas = " → ".join(f"{i+1}) {d['tienda']}" for i, d in enumerate(destinos_viaje))
            registrar_auditoria(usuario, "Crear viaje", f"Viaje {id_viaje_str} · Cliente {cliente} · Placa {placa} · Orden de paradas: {orden_tiendas}")
            return True, id_viaje_str
        except psycopg2.IntegrityError as e:
            conn.rollback()
            return False, "Ese marchamo ya está en uso en otro viaje — revisa el número e inténtalo de nuevo."
        except Exception as e:
            conn.rollback()
            return False, _error_tecnico(e, "guardar_viaje")


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
                ag.bultos AS "Bultos"
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


def _sanear_formulas(df):
    """Neutraliza el riesgo de 'CSV/Excel Injection': si una celda de texto
    empieza con =, +, -, @ (o tab/retorno de carro), Excel podría interpretarla
    como una fórmula al abrir el archivo exportado — por ejemplo alguien
    escribiendo eso a propósito en una Observación. Le anteponemos un
    apóstrofo para que Excel la trate siempre como texto plano, nunca como
    fórmula. No cambia el dato guardado en la base, solo el archivo exportado."""
    df = df.copy()
    caracteres_riesgosos = ("=", "+", "-", "@", "\t", "\r")
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].apply(
            lambda v: "'" + v if isinstance(v, str) and v.startswith(caracteres_riesgosos) else v
        )
    return df


def exportar_excel(df):
    buffer = io.BytesIO()
    _sanear_formulas(df).to_excel(buffer, index=False, engine="openpyxl")
    return buffer.getvalue()


def exportar_csv(df):
    return _sanear_formulas(df).to_csv(index=False).encode("utf-8-sig")


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


def obtener_reporte_retornable_por_fecha(fecha_inicio, fecha_fin, cliente="Todos"):
    """Detalle día por día: en cada fecha del viaje, cuánto se envió y cuánto se
    retornó de cada material, por tienda. Todo queda bajo la fecha del viaje
    (no la de liquidación) para que no haya confusión al leerlo día a día —
    el retorno solo tiene valor una vez que ese viaje ya se liquidó, pero se
    reporta en la misma fila que su envío."""
    with closing(get_conn()) as conn:
        query = """
            SELECT v.fecha_creacion AS "Fecha", v.cliente AS "Cliente", d.tienda AS "Tienda",
                   SUM(d.roles) AS "Roles Enviados",
                   SUM(COALESCE(d.roles_devueltos, 0)) AS "Roles Retornados",
                   SUM(d.tarimas) AS "Tarimas Enviadas",
                   SUM(COALESCE(d.tarimas_devueltas, 0)) AS "Tarimas Retornadas",
                   SUM(COALESCE(d.pacas_carton_devueltas, 0)) AS "Pacas de Cartón Retornadas"
            FROM destinos d JOIN viajes v ON v.id = d.viaje_id
            WHERE v.estado != 'Anulado' AND v.fecha_creacion BETWEEN %s AND %s
              AND (%s = 'Todos' OR v.cliente = %s)
            GROUP BY v.fecha_creacion, v.cliente, d.tienda
            ORDER BY v.cliente, d.tienda, v.fecha_creacion
        """
        df = pd.read_sql_query(query, conn, params=(str(fecha_inicio), str(fecha_fin), cliente, cliente))
        # Cada paca de cartón equivale a 50 lbs (acordado con el cliente) —
        # se agrega como columna aparte, sin quitar el conteo de pacas, para
        # no perder la unidad con la que realmente se digitó.
        df["Lbs de Cartón"] = df["Pacas de Cartón Retornadas"] * LBS_POR_PACA_CARTON
        return df


def obtener_plan_carga(fecha):
    """Trae el plan de carga (camiones/bultos por hora) de una fecha, con las
    24 horas siempre presentes (en 0 si todavía no se ha cargado nada)."""
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT hora, camiones_plan, bultos_plan FROM plan_carga_horario WHERE fecha = %s",
            (fecha,)
        )
        existentes = {r["hora"]: r for r in cur.fetchall()}
    return [
        {"Hora": f"{h:02d}:00", "Camiones Plan": existentes.get(h, {}).get("camiones_plan", 0) or 0,
         "Bultos Plan": existentes.get(h, {}).get("bultos_plan", 0) or 0}
        for h in range(24)
    ]


def guardar_plan_carga(fecha, filas, usuario):
    """Guarda el plan de carga de una fecha — upsert por hora, nunca borra
    nada fuera de las 24 horas que ya se están mandando."""
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                for i, fila in enumerate(filas):
                    cur.execute(
                        "INSERT INTO plan_carga_horario (fecha, hora, camiones_plan, bultos_plan) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT (fecha, hora) DO UPDATE SET "
                        "camiones_plan = EXCLUDED.camiones_plan, bultos_plan = EXCLUDED.bultos_plan",
                        (fecha, i, fila["Camiones Plan"], fila["Bultos Plan"])
                    )
            conn.commit()
            registrar_auditoria(usuario, "Guardar plan de carga", f"Fecha {fecha}")
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, _error_tecnico(e, "guardar_plan_carga")



    """Busca viajes por coincidencia PARCIAL (no exacta) de No. de Viaje, Marchamo de
    Ida (de cualquiera de sus destinos), o Placa. Devuelve una lista (puede tener
    más de un resultado si el texto buscado coincide con varios viajes).
    Si `clientes_permitidos` no es None, solo devuelve viajes de esos clientes —
    así nadie encuentra, ni por accidente, un viaje de un cliente que no le
    corresponde (Administrador pasa None y ve todo)."""
    valor = f"%{valor_busqueda.strip()}%"
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT v.* FROM viajes v
            WHERE (v.id_viaje ILIKE %s OR v.placa ILIKE %s
               OR EXISTS (SELECT 1 FROM destinos d WHERE d.viaje_id = v.id AND d.marchamo_ida ILIKE %s))
              AND (%s::text[] IS NULL OR v.cliente = ANY(%s))
            ORDER BY v.id DESC LIMIT 20
        """, (valor, valor, valor, clientes_permitidos, clientes_permitidos))
        return cur.fetchall()


def obtener_destinos_de_viaje(viaje_id):
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM destinos WHERE viaje_id = %s ORDER BY orden", (viaje_id,))
        return cur.fetchall()


def buscar_viajes(valor_busqueda, clientes_permitidos=None):
    """Búsqueda libre por No. de Viaje, Marchamo de Ida o Placa — no hace falta
    escribirlo completo (usa coincidencia parcial, tipo 'contiene'). Respeta
    `clientes_permitidos` si se pasa, igual que filtrar_viajes()."""
    patron = f"%{str(valor_busqueda).strip()}%"
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT v.* FROM viajes v
            LEFT JOIN destinos d ON d.viaje_id = v.id
            WHERE (v.id_viaje ILIKE %s OR v.placa ILIKE %s OR d.marchamo_ida ILIKE %s)
              AND (%s::text[] IS NULL OR v.cliente = ANY(%s))
            ORDER BY v.id DESC LIMIT 50
        """, (patron, patron, patron, clientes_permitidos, clientes_permitidos))
        return cur.fetchall()


def filtrar_viajes(estado="Todos", placa="Todas", limite=50, clientes_permitidos=None):
    """Filtro rápido por Estado y/o Placa, para encontrar viajes sin tener que
    escribir un texto exacto de búsqueda. Igual que buscar_viajes(), respeta
    `clientes_permitidos` si se pasa."""
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM viajes
            WHERE (%s = 'Todos' OR estado = %s)
              AND (%s = 'Todas' OR placa = %s)
              AND (%s::text[] IS NULL OR cliente = ANY(%s))
            ORDER BY id DESC LIMIT %s
        """, (estado, estado, placa, placa, clientes_permitidos, clientes_permitidos, limite))
        return cur.fetchall()


def anular_viaje(viaje_id, usuario, motivo, liberar_marchamos=True):
    """Marca el viaje como Anulado (no lo borra, queda como registro para auditoría).
    Por defecto libera los marchamos de sus destinos para que puedan reutilizarse en
    otro viaje, ya que un viaje anulado normalmente significa un error de digitación,
    no un marchamo físicamente gastado. Esto es ajustable con liberar_marchamos=False.
    Si el viaje YA estaba Anulado, no hace nada — evita doblar el sufijo del marchamo
    y perder el motivo/fecha de la anulación original."""
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                fecha_hoy = ahora().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    "UPDATE viajes SET estado='Anulado', usuario_anulo=%s, "
                    "fecha_anulacion=%s, motivo_anulacion=%s WHERE id=%s AND estado != 'Anulado'",
                    (usuario, fecha_hoy, motivo, viaje_id)
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return False, "Este viaje ya estaba Anulado — alguien más lo anuló mientras tenías esta pantalla abierta."
                if liberar_marchamos:
                    # Libera los marchamos poniéndolos como usados-pero-anulados con un
                    # sufijo único, para que marchamo_ya_usado() ya no los bloquee.
                    cur.execute(
                        "UPDATE destinos SET marchamo_ida = marchamo_ida || '-ANULADO-' || id::text "
                        "WHERE viaje_id = %s", (viaje_id,)
                    )
            conn.commit()
            registrar_auditoria(usuario, "Anular viaje", f"Viaje ID {viaje_id} · Motivo: {motivo}")
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, _error_tecnico(e, "anular_viaje")


def editar_viaje(viaje_id, placa, transportista, piloto, auxiliar, destinos_actualizados, marchamo_regreso_viaje, usuario_editor):
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

                # Lo mismo para el Marchamo de Regreso: no debe chocar con el de
                # otro viaje (excluyendo los destinos de este mismo viaje).
                if marchamo_regreso_viaje:
                    ids_de_este_viaje = [d["id"] for d in destinos_actualizados]
                    cur.execute(
                        "SELECT 1 FROM destinos WHERE (marchamo_ida = %s OR marchamo_regreso = %s) AND id != ALL(%s)",
                        (marchamo_regreso_viaje, marchamo_regreso_viaje, ids_de_este_viaje)
                    )
                    if cur.fetchone():
                        conn.rollback()
                        return False, f"El marchamo de regreso '{marchamo_regreso_viaje}' ya está en uso en otro viaje."

                cur.execute(
                    "UPDATE viajes SET placa=%s, transportista=%s, piloto=%s, auxiliar=%s "
                    "WHERE id=%s AND estado = 'Pendiente de Liquidar'",
                    (placa, transportista, piloto, auxiliar, viaje_id)
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return False, "Este viaje ya no está 'Pendiente de Liquidar' — alguien más lo liquidó o anuló mientras tenías esta pantalla abierta, así que ya no se puede editar."
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
            orden_tiendas_edit = " → ".join(f"{d['orden']}) {d['tienda']}" for d in sorted(destinos_actualizados, key=lambda x: x["orden"]))
            registrar_auditoria(usuario_editor, "Editar viaje", f"Viaje ID {viaje_id} · Placa {placa} · Orden de paradas: {orden_tiendas_edit}")
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, _error_tecnico(e, "editar_viaje")


def generar_hoja_control_html(viaje, destinos):
    """Arma la Hoja de Control de Viaje como HTML: en pantalla se ve con los colores
    de Ransa, pero al imprimir (@media print) los fondos de color se vuelven blancos
    y solo quedan bordes negros, para que salga limpia en una impresora blanco y negro.
    Si el viaje ya está Liquidado, los recuadros de devolución se llenan con los datos
    reales de la liquidación en vez de salir en blanco.

    Todo texto que pudo haber sido digitado por una persona (marchamos, tienda,
    observaciones, no. de despacho, documentos) se pasa por esc() antes de meterlo
    en el HTML — si no, alguien podría escribir código en un campo de texto y que
    se ejecute en la pantalla de quien reimprima esa hoja después (Administrador
    incluido). esc() nunca cambia el dato que se guarda en la base, solo lo que
    se muestra."""
    def esc(valor):
        return html_lib.escape(str(valor)) if valor is not None else ""

    marchamo_ida_general = esc(destinos[0]["marchamo_ida"]) if destinos else ""
    es_cliente_unisuper = viaje["cliente"].startswith("UniSuper")
    esta_liquidado = viaje["estado"] == "Liquidado"

    bloques_destino = ""
    for idx, d in enumerate(destinos, start=1):
        try:
            pedidos_lista = json.loads(d["pedidos"]) if d["pedidos"] else []
        except (json.JSONDecodeError, TypeError):
            pedidos_lista = []
        pedidos_txt = esc(", ".join(p["pedido"] for p in pedidos_lista)) if pedidos_lista else "—"
        badge_regreso = (
            f'<span class="badge-regreso">Marchamo Retorno: {esc(d["marchamo_regreso"])}</span>'
            if d["marchamo_regreso"] else ""
        )
        badge_complemento = '<span class="badge-complemento">COMPLEMENTO</span>' if d["es_complemento"] else ""
        incidencia_txt = esc(d["incidencias"]) if d["incidencias"] else ""
        incidencia_html = f'<div class="incidencia">⚠ {incidencia_txt}</div>' if incidencia_txt else ""
        material_cajas = (
            f'<div class="material-box"><b>{d["cajas"]}</b><span>BULTOS</span></div>'
            if not d["es_complemento"] else ""
        )
        documentos_html = (
            f'<div class="sub-info">Remisión: <b>{esc(d["remitos"]) or "—"}</b> &nbsp;|&nbsp;'
            f'Devolución: <b>{esc(d["devolucion"]) or "—"}</b> &nbsp;|&nbsp;'
            f'Créditos: <b>{esc(d["creditos"]) or "—"}</b> &nbsp;|&nbsp;'
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
                <div class="destino-header-izq">
                    <span class="destino-num">{idx}</span> {esc(d['tienda'])}
                    <span class="marchamo-inline">Marchamo: {esc(d['marchamo_ida'])}</span>
                    {badge_complemento}
                </div>
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
                    <div class="sub-info sub-info-grande">No. de Despachos: <b>{pedidos_txt}</b></div>
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
                        <span>ROLES</span><span>TARIMAS</span><span>PACAS DE CARTÓN</span>
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
        .destino-header {{ background: #eef6ef; padding: 4px 10px; font-weight: bold; font-size: 12px;
                            display: flex; align-items: center; justify-content: space-between;
                            flex-wrap: wrap; gap: 6px; }}
        .destino-header-izq {{ display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }}
        .destino-num {{ background: #0B4A32; color: white; border-radius: 50%; padding: 1px 7px; font-size: 11px; }}
        .marchamo-inline {{ font-size: 13px; font-weight: bold; color: #0B4A32;
                             background: #fff; border: 1px solid #0B4A32; border-radius: 4px; padding: 3px 12px; }}
        .badge-regreso {{ background: #B5622E; color: white; padding: 3px 12px;
                           border-radius: 4px; font-size: 13px; font-weight: bold; white-space: nowrap; }}
        .badge-complemento {{ background: #7A3E1D; color: white; padding: 1px 8px;
                               border-radius: 4px; font-size: 10px; }}
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
                   por <b>{esc(viaje['usuario_creador'])}</b></p>
                {f'<p style="margin-top:2px; color:#0B4A32;">✅ Liquidado por <b>{esc(viaje["usuario_liquido"])}</b> el <b>{viaje["fecha_liquidacion"]} {viaje["hora_liquidacion"]}</b></p>' if esta_liquidado else ''}
            </div>
        </div>
        <div class="datos-grid">
            <div class="dato"><label>No. de Viaje</label><span>{esc(viaje['id_viaje'])}</span></div>
            <div class="dato"><label>Cliente</label><span>{esc(viaje['cliente'])}</span></div>
            <div class="dato"><label>CD Origen</label><span>{esc(viaje['cd_origen']) or '—'}</span></div>
            <div class="dato"><label>Transportista</label><span>{esc(viaje['transportista'])}</span></div>
            <div class="dato"><label>Placa</label><span>{esc(viaje['placa'])}</span></div>
            <div class="dato"><label>Fecha</label><span>{viaje['fecha_creacion']}</span></div>
            <div class="dato dato-blanco"><label>Horario (Garita — hora real de salida)</label><span>&nbsp;</span></div>
            <div class="dato"><label>Piloto</label><span>{esc(viaje['piloto'])}</span></div>
            <div class="dato"><label>Auxiliar</label><span>{esc(viaje['auxiliar'])}</span></div>
            <div class="dato"><label>Marchamo de Ida</label><span>{marchamo_ida_general}</span></div>
        </div>
        <div class="titulo-destinos">DESTINOS DEL VIAJE ({len(destinos)})</div>
        {bloques_destino}
        <div class="footer">
            <span>Hoja generada por el sistema Control de Ruta · Ransa · v{VERSION_APP}</span>
            <span>Sellar y entregar al finalizar el viaje para su liquidación.</span>
        </div>
    </div>
    </body>
    </html>
    """


def liquidar_viaje(viaje_id, destinos_actualizados, usuario):
    """Registra lo que el camión trajo de regreso por cada destino y marca el viaje como Liquidado.
    destinos_actualizados: lista de dicts con id, roles_devueltos, tarimas_devueltas, pacas_carton_devueltas.
    Solo aplica si el viaje SIGUE Pendiente de Liquidar en este momento — si alguien más
    ya lo liquidó o anuló mientras esta pantalla estaba abierta, no se pisa nada."""
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
                    "fecha_liquidacion=%s, hora_liquidacion=%s "
                    "WHERE id=%s AND estado='Pendiente de Liquidar'",
                    (usuario, fecha_hoy, hora_hoy, viaje_id)
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return False, "Este viaje ya no está 'Pendiente de Liquidar' — alguien más lo liquidó o anuló mientras tenías esta pantalla abierta. Refresca y revisa su estado actual."
            conn.commit()
            registrar_auditoria(usuario, "Liquidar viaje", f"Viaje ID {viaje_id}")
            return True, "OK"
        except Exception as e:
            conn.rollback()
            return False, _error_tecnico(e, "liquidar_viaje")


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
    # Esto corre ANTES del login — nadie autenticado todavía — así que aquí
    # nunca se muestra el detalle técnico, solo dónde/cuándo/qué tipo, igual
    # que el resto de errores — para que una captura de esto ya sirva.
    fecha_hora_conexion = ahora().strftime("%Y-%m-%d %H:%M:%S")
    tipo_error_conexion = type(e).__name__
    print(f"[ERROR {fecha_hora_conexion}] conexion_bd_previo_a_login ({tipo_error_conexion}): {e}")
    st.sidebar.error(f"🔴 Sin conexión a la base de datos.\n\n**Tipo:** `{tipo_error_conexion}`  \n**Cuándo:** `{fecha_hora_conexion}`")
    st.stop()

# --- PANTALLA 1: LOGIN ---
# Usuario y contraseña reales: la contraseña se valida contra un hash seguro
# guardado en la base de datos (nunca en texto plano). El campo de usuario es
# de texto libre, no una lista desplegable, para no exponer a cualquier
# visitante qué nombres de usuario existen en el sistema.
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
            usuario_login = st.text_input("Usuario")
            password_login = st.text_input("Contraseña", type="password")
            if st.button(":material/login: Ingresar al Sistema", use_container_width=True):
                resultado_login = verificar_login(usuario_login.strip(), password_login) if usuario_login.strip() else None
                if resultado_login:
                    st.session_state["usuario_activo_fijo"] = usuario_login.strip()
                    st.session_state["perfil_activo_fijo"] = resultado_login["perfil"]
                    st.session_state["debe_cambiar_password"] = resultado_login["debe_cambiar_password"]
                    st.session_state["login_confirmado"] = True
                    st.rerun()
                else:
                    st.error("❌ Usuario o contraseña incorrectos.")
    st.stop()

usuario_activo = st.session_state["usuario_activo_fijo"]
perfil_activo = st.session_state["perfil_activo_fijo"]

# Se revisa en CADA acción, no solo al entrar — así, si un Administrador
# desactiva esta cuenta mientras ya está trabajando, se cierra la sesión de
# inmediato en vez de esperar a que la persona cierre sesión por su cuenta.
if not cuenta_sigue_activa(usuario_activo):
    st.error("🚫 Tu cuenta fue desactivada. Contacta a un Administrador si crees que es un error.")
    for k in ("usuario_activo_fijo", "perfil_activo_fijo", "debe_cambiar_password",
              "login_confirmado", "config_bloqueada", "cliente_activo_fijo", "cd_origen_fijo"):
        st.session_state.pop(k, None)
    st.stop()

# Cierre de sesión automático tras 30 minutos sin ninguna acción — para que una
# sesión abierta y olvidada en una computadora compartida (garita, bodega) no
# se quede activa indefinidamente. Cada clic/acción cuenta como actividad y
# reinicia el conteo.
MINUTOS_INACTIVIDAD_MAXIMOS = 30
ahora_actividad = ahora().replace(tzinfo=None)
ultima_actividad = st.session_state.get("ultima_actividad")
if ultima_actividad and (ahora_actividad - ultima_actividad).total_seconds() > MINUTOS_INACTIVIDAD_MAXIMOS * 60:
    registrar_auditoria(usuario_activo, "Sesión cerrada por inactividad", f"Más de {MINUTOS_INACTIVIDAD_MAXIMOS} minutos sin actividad")
    for k in ("usuario_activo_fijo", "perfil_activo_fijo", "debe_cambiar_password",
              "login_confirmado", "config_bloqueada", "cliente_activo_fijo", "cd_origen_fijo", "ultima_actividad"):
        st.session_state.pop(k, None)
    st.warning(f"⏱️ Tu sesión se cerró automáticamente por {MINUTOS_INACTIVIDAD_MAXIMOS} minutos sin actividad. Vuelve a entrar.")
    st.stop()
st.session_state["ultima_actividad"] = ahora_actividad

# --- PANTALLA 1B: Cambio de contraseña obligatorio (primer ingreso, o tras un
# restablecimiento). No se puede pasar de aquí sin poner una contraseña nueva.
if st.session_state.get("debe_cambiar_password"):
    st.markdown("""
        <div class="ransa-topbar" style="justify-content:center;">
            <div style="text-align:center;">
                <div class="titulo">:material/key: Cambio de Contraseña Obligatorio</div>
                <div class="subtitulo">Define una contraseña nueva para continuar</div>
            </div>
        </div>
    """, unsafe_allow_html=True)
    col_izq2, col_centro2, col_der2 = st.columns([1, 1.2, 1])
    with col_centro2:
        with st.container(border=True):
            nueva1 = st.text_input("Nueva contraseña (mínimo 8 caracteres, con letra y número)", type="password", key="nueva_pw_1")
            nueva2 = st.text_input("Repite la nueva contraseña", type="password", key="nueva_pw_2")
            if st.button(":material/check: Guardar Contraseña", use_container_width=True):
                valida, msg_valida = password_es_valida(nueva1)
                if not valida:
                    st.error(f"❌ {msg_valida}")
                elif nueva1 != nueva2:
                    st.error("❌ Las dos contraseñas no coinciden.")
                else:
                    ok, msg = establecer_password(usuario_activo, nueva1, forzar_cambio_siguiente=False)
                    if ok:
                        st.session_state["debe_cambiar_password"] = False
                        st.success("✅ Contraseña actualizada.")
                        st.rerun()
                    else:
                        mostrar_resultado_error(msg, perfil_activo)
    st.stop()

st.sidebar.success(f"👤 **{usuario_activo}**")
st.sidebar.caption(f"Perfil: {perfil_activo}")
with st.sidebar.expander(":material/key: Cambiar mi contraseña"):
    pw_actual = st.text_input("Contraseña actual", type="password", key="pw_actual_sidebar")
    pw_nueva1 = st.text_input("Nueva contraseña (con letra y número)", type="password", key="pw_nueva1_sidebar")
    pw_nueva2 = st.text_input("Repite la nueva contraseña", type="password", key="pw_nueva2_sidebar")
    if st.button("Actualizar Contraseña", key="btn_pw_sidebar"):
        valida, msg_valida = password_es_valida(pw_nueva1)
        if not _credenciales_validas(usuario_activo, pw_actual):
            st.error("❌ La contraseña actual no es correcta.")
        elif not valida:
            st.error(f"❌ {msg_valida}")
        elif pw_nueva1 != pw_nueva2:
            st.error("❌ Las dos contraseñas nuevas no coinciden.")
        else:
            ok, msg = establecer_password(usuario_activo, pw_nueva1, forzar_cambio_siguiente=False)
            if ok:
                st.success("✅ Contraseña actualizada.")
            else:
                mostrar_resultado_error(msg, perfil_activo)
if st.sidebar.button(":material/logout: Cerrar Sesión"):
    st.session_state["login_confirmado"] = False
    st.session_state["config_bloqueada"] = False
    for k in ("usuario_activo_fijo", "perfil_activo_fijo", "debe_cambiar_password", "cliente_activo_fijo", "cd_origen_fijo"):
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
    # Aunque el usuario tenga acceso a un cliente, si ese cliente está
    # Inactivo no debería poder empezar a despachar viajes nuevos para él —
    # los Reportes y clientes_permitidos_para() sí lo siguen viendo (para no
    # perder el historial), pero aquí, para EMPEZAR A TRABAJAR, no aplica.
    clientes_activos_set = set(st.session_state.catalogos["clientes_lista_activos"])
    clientes_disp = [c for c in clientes_disp if c in clientes_activos_set]
    if not clientes_disp:
        st.error("🚫 Tu usuario no tiene ningún cliente activo asignado. Pídele a un Administrador que te "
                 "dé acceso desde la pestaña de Catálogos ('Acceso Usuario → Cliente'), o que reactive el "
                 "cliente si está marcado como Inactivo.")
        st.stop()

    cliente_activo_sel = st.sidebar.selectbox("🎯 Cliente", clientes_disp)

    cds_disponibles = st.session_state.catalogos["cds_por_cliente"].get(cliente_activo_sel, [])
    if len(cds_disponibles) <= 1:
        # Un solo CD posible: se toma como elegido, sin mostrar nada — no hace
        # falta pedirle confirmación al usuario por algo que no tiene otra opción.
        cd_origen_sel = cds_disponibles[0] if cds_disponibles else ""
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
            <b>{html_lib.escape(str(cliente_activo))}</b> · CD {html_lib.escape(str(cd_origen_fijo))}<br>
            {html_lib.escape(str(usuario_activo))} ({html_lib.escape(str(perfil_activo))}) · {ahora().strftime('%H:%M:%S')}
        </div>
    </div>
""", unsafe_allow_html=True)
st.markdown("---")

tab1, tab2, tab5, tab3, tab4, tab6 = st.tabs([
    ":material/local_shipping: Despacho (Salidas)",
    ":material/receipt_long: Recepción (Liquidaciones)",
    ":material/edit_document: Gestión de Viajes",
    ":material/bar_chart: Reportes",
    ":material/settings: Catálogos",
    ":material/manage_accounts: Usuarios"
])

# ==========================================
# MÓDULO 1: DESPACHO / CREACIÓN DE VIAJES
# ==========================================
with tab1:
    if perfil_activo in ["Administrador", "SuperAdministrador", "Operador", "Supervisor"]:
        st.header(":material/local_shipping: Creación de Viaje")
        st.caption(f"Configura placa, ruta y materiales del nuevo viaje · Digitando como **{usuario_activo}** ({perfil_activo})")

        run = st.session_state.form_run  # sufijo de las keys del formulario actual
        marchamo_regreso_actual = st.session_state.get(f"mreg_final_{run}", "")
        tiendas_cliente = st.session_state.catalogos["clientes"].get(cliente_activo, {})
        es_cliente_unisuper = cliente_activo.startswith("UniSuper")

        if not tiendas_cliente:
            st.warning(f"⚠️ El cliente **{cliente_activo}** todavía no tiene ninguna tienda cargada en el "
                       "catálogo de Clientes y Tiendas — no se puede despachar sin al menos una. "
                       "Pídele a un Administrador o Supervisor que las cargue en la pestaña Catálogos.")
            st.stop()

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
                    if pilotos:
                        piloto_final = st.selectbox("Piloto", pilotos, index=pilotos.index(pil_pred) if pil_pred in pilotos else 0, key=f"piloto_{run}")
                    else:
                        st.warning("Sin pilotos en el catálogo.")
                        piloto_final = ""
                with col_aux:
                    auxiliares = st.session_state.catalogos["auxiliares"]
                    if auxiliares:
                        auxiliar_final = st.selectbox("Auxiliar de Carga", auxiliares, index=auxiliares.index(aux_pred) if aux_pred in auxiliares else 0, key=f"aux_{run}")
                    else:
                        st.warning("Sin auxiliares en el catálogo.")
                        auxiliar_final = ""

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
                                    pedido_codigo = st.text_input("No. de Despacho", key=f"cod_pedido_{run}_{i}_{subrun}")
                                with fp2:
                                    pedido_cajas = st.number_input("Bultos del despacho", min_value=0, step=1, value=None, placeholder="0", key=f"cajas_pedido_{run}_{i}_{subrun}") or 0
                                with fp3:
                                    st.write("")
                                    agregar_pedido = st.button(":material/add: Agregar Despacho", key=f"btn_agregar_pedido_{run}_{i}_{subrun}", use_container_width=True)
                                if agregar_pedido:
                                    if not pedido_codigo.strip():
                                        st.warning("Escribe un número de despacho antes de agregarlo.")
                                    elif pedido_cajas <= 0:
                                        st.warning("⚠️ Ese despacho no tiene bultos — indica cuántos bultos trae antes de agregarlo.")
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
                                    st.number_input("Bultos Totales", value=0, disabled=True, key=f"c_disabled_{run}_{i}")
                                elif lista_pedidos:
                                    cajas_total = sum(p["cajas"] for p in lista_pedidos)
                                    st.number_input("Bultos Totales", value=cajas_total, disabled=True, key=f"c_calc_{run}_{i}")
                                else:
                                    cajas_total = st.number_input("Bultos Totales", min_value=0, step=1, value=None, placeholder="0", key=f"c_{run}_{i}") or 0
                            with mf2:
                                tarimas = st.number_input("Tarimas", min_value=0, step=1, value=None, placeholder="0", key=f"tar_{run}_{i}") or 0
                            with mf3:
                                roles = st.number_input("Roles Secos", min_value=0, step=1, value=None, placeholder="0", key=f"r_{run}_{i}") or 0
                            with mf4:
                                # Ya no se elige aquí — es un dato fijo configurado en el
                                # catálogo de Tiendas, y esta pantalla solo lo muestra.
                                tipo_pago = tiendas_cliente[tienda]["clasificacion"] if tienda else "Local"
                                st.text_input("Clasificación de Destino", value=tipo_pago, disabled=True, key=f"tipopago_{run}_{i}")

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

                st.markdown("---")
                cc1, cc2 = st.columns([2, 1])
                with cc1:
                    marchamo_regreso_viaje = st.text_input(
                        ":material/lock: Marchamo de Regreso (obligatorio, se cierra al terminar la última tienda)",
                        key=f"mreg_final_{run}", placeholder="Marchamo de Regreso"
                    )
                if destinos_viaje:
                    destinos_viaje[-1]["marchamo_regreso"] = marchamo_regreso_viaje.strip()
                with cc2:
                    st.write("")
                    guardar_click = st.button(":material/print: Generar Viaje e Imprimir", use_container_width=True, type="primary")

        with col_side:
            with st.container(border=True):
                st.markdown("##### :material/summarize: RESUMEN DE DATOS")
                total_tarimas = sum(d["tarimas"] for d in destinos_viaje)
                total_roles = sum(d["roles"] for d in destinos_viaje)
                total_cajas = sum(d["cajas"] for d in destinos_viaje)
                cantidad_tiendas = len(destinos_viaje)
                distancia_total = round(sum(d["km"] for d in destinos_viaje), 1)

                resumen_items = [
                    ("Bultos", total_cajas), ("Tarimas", total_tarimas),
                    ("Roles", total_roles), ("Tiendas", cantidad_tiendas),
                ]
                rcols = st.columns(len(resumen_items))
                for rcol, (etiqueta, valor) in zip(rcols, resumen_items):
                    with rcol:
                        st.markdown(
                            f'<div class="resumen-chip"><div class="resumen-chip-label">{etiqueta}</div>'
                            f'<div class="resumen-chip-valor">{valor}</div></div>',
                            unsafe_allow_html=True
                        )
                st.caption(f":material/route: Distancia total: {distancia_total} KM")

                st.markdown("---")
                st.caption(":material/preview: VISTA PREVIA — HOJA DE SALIDA")
                if not destinos_viaje:
                    st.caption("Agrega un destino para ver la vista previa.")
                else:
                    for idx, d in enumerate(destinos_viaje, start=1):
                        st.markdown(f"**{idx}. {d['tienda'] or '—'}**")
                        st.caption(f"Marchamo Ida: {d['marchamo_ida'] or '—'}")
                        st.caption(f"Tarimas: {d['tarimas']} · Roles: {d['roles']} · Bultos: {d['cajas']}")
                        if idx == len(destinos_viaje) and marchamo_regreso_actual:
                            st.markdown(f":material/lock: **Marchamo Regreso:** {marchamo_regreso_actual}")
                        st.markdown("---")

        if guardar_click:
            marchamos_vacios = any(not d["marchamo_ida"] for d in destinos_viaje)
            marchamos_repetidos_en_form = len([d["marchamo_ida"] for d in destinos_viaje]) != len(
                set(d["marchamo_ida"] for d in destinos_viaje)
            )
            marchamo_regreso_choca_en_form = marchamo_regreso_viaje.strip() and any(
                d["marchamo_ida"] == marchamo_regreso_viaje.strip() for d in destinos_viaje
            )

            if not placa or len(destinos_viaje) == 0:
                st.error("❌ Error: Debe seleccionar el camión y al menos un destino.")
            elif not piloto_final or not auxiliar_final:
                st.error("❌ Error: Falta seleccionar Piloto y/o Auxiliar (revisa que el catálogo tenga al menos uno cargado).")
            elif marchamos_vacios:
                st.error("❌ Error: Todos los destinos ingresados deben tener un Marchamo de Ida asignado.")
            elif marchamos_repetidos_en_form:
                st.error("❌ Error: Hay marchamos de ida repetidos dentro de este mismo viaje.")
            elif not marchamo_regreso_viaje.strip():
                st.error("❌ Error: El Marchamo de Regreso es obligatorio para cerrar el circuito.")
            elif marchamo_regreso_choca_en_form:
                st.error("❌ Error: El Marchamo de Regreso no puede ser igual a un Marchamo de Ida de este mismo viaje.")
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
                    mostrar_resultado_error(resultado, perfil_activo)

        if st.session_state.get("ultimo_viaje_guardado"):
            st.markdown("---")
            st.success(f"✅ Viaje {st.session_state['ultimo_viaje_guardado']} generado — revisa la Hoja de Control abajo e imprímela si corresponde.")
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
    if perfil_activo in ["Administrador", "SuperAdministrador", "Liquidador", "Supervisor"]:
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
                mis_clientes_liq = None if perfil_activo in ("Administrador", "SuperAdministrador") else clientes_permitidos_para(usuario_activo, perfil_activo)
                st.session_state["filtrados_liq"] = filtrar_viajes(filtro_estado_liq, filtro_placa_liq, clientes_permitidos=mis_clientes_liq)

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
                mis_clientes_liq2 = None if perfil_activo in ("Administrador", "SuperAdministrador") else clientes_permitidos_para(usuario_activo, perfil_activo)
                resultados = buscar_viajes(valor_busqueda, clientes_permitidos=mis_clientes_liq2)
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
                        mostrar_resultado_error(msg, perfil_activo)
    else:
        st.info("Tu perfil no tiene permisos para liquidar viajes.")

# ==========================================
# MÓDULO 2B: GESTIÓN DE VIAJES — Editar (solo si Pendiente de Liquidar) y Anular
# (disponible mientras no esté ya Anulado). Separado de Liquidaciones a propósito.
# ==========================================
with tab5:
    if perfil_activo in ["Administrador", "SuperAdministrador", "Operador"]:
        st.header(":material/edit_document: Gestión de Viajes")
        if perfil_activo == "Operador":
            st.caption("Puedes corregir o anular cualquier viaje de los clientes que tengas asignados "
                       "(no solo los que tú mismo creaste — para que un turno pueda corregir lo del otro). "
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
                mis_clientes_g = None if perfil_activo in ("Administrador", "SuperAdministrador") else clientes_permitidos_para(usuario_activo, perfil_activo)
                st.session_state["resultados_gestion"] = buscar_viajes(valor_busqueda_g, clientes_permitidos=mis_clientes_g)
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

            # Antes esto era "solo lo que tú mismo creaste" — se cambió porque tu
            # operación trabaja por turnos (uno despacha, el otro corrige o cierra),
            # así que ahora se valida por acceso al Cliente, no por autoría.
            es_propietario = (perfil_activo in ("Administrador", "SuperAdministrador")) or (viaje_g["cliente"] in clientes_permitidos_para(usuario_activo, perfil_activo))
            if not es_propietario:
                st.warning("🚫 No tienes acceso al cliente de este viaje, así que no lo puedes editar ni anular.")
            elif viaje_g["estado"] == "Anulado":
                st.error(f"🚫 Este viaje ya fue anulado el {viaje_g['fecha_anulacion']} por "
                         f"{viaje_g['usuario_anulo']}. Motivo: {viaje_g['motivo_anulacion']}. No hay más acciones disponibles.")
            else:
                if viaje_g["estado"] == "Pendiente de Liquidar":
                    with st.expander("✏️ Editar este viaje (corregir datos digitados)", expanded=True):
                        st.caption("No. de Viaje, Cliente, y quién/cuándo se creó NO se pueden cambiar.")
                        tiendas_cliente_g = st.session_state.catalogos["clientes"].get(viaje_g["cliente"], {})

                        placas_disp = list(st.session_state.catalogos["camiones"].keys())
                        if placas_disp:
                            placa_idx = placas_disp.index(viaje_g["placa"]) if viaje_g["placa"] in placas_disp else 0
                            placa_edit = st.selectbox("Placa", placas_disp, index=placa_idx, key=f"edit_placa_{viaje_g['id']}")
                        else:
                            st.warning(f"Catálogo de Camiones vacío — se mantiene la placa actual: {viaje_g['placa']}")
                            placa_edit = viaje_g["placa"]
                        datos_cam = st.session_state.catalogos["camiones"].get(placa_edit, {})
                        transportista_edit = datos_cam.get("transportista", viaje_g["transportista"])

                        pilotos_disp = st.session_state.catalogos["pilotos"]
                        if pilotos_disp:
                            pil_idx = pilotos_disp.index(viaje_g["piloto"]) if viaje_g["piloto"] in pilotos_disp else 0
                            piloto_edit = st.selectbox("Piloto", pilotos_disp, index=pil_idx, key=f"edit_piloto_{viaje_g['id']}")
                        else:
                            st.warning(f"Catálogo de Pilotos vacío — se mantiene el piloto actual: {viaje_g['piloto']}")
                            piloto_edit = viaje_g["piloto"]

                        aux_disp = st.session_state.catalogos["auxiliares"]
                        if aux_disp:
                            aux_idx = aux_disp.index(viaje_g["auxiliar"]) if viaje_g["auxiliar"] in aux_disp else 0
                            auxiliar_edit = st.selectbox("Auxiliar", aux_disp, index=aux_idx, key=f"edit_aux_{viaje_g['id']}")
                        else:
                            st.warning(f"Catálogo de Auxiliares vacío — se mantiene el auxiliar actual: {viaje_g['auxiliar']}")
                            auxiliar_edit = viaje_g["auxiliar"]

                        st.markdown("##### DATOS POR TIENDA")
                        destinos_editados = []
                        for d in sorted(destinos_g, key=lambda x: x["orden"]):
                            tiendas_opciones = list(tiendas_cliente_g.keys())
                            if tiendas_opciones:
                                tienda_idx = tiendas_opciones.index(d["tienda"]) if d["tienda"] in tiendas_opciones else 0
                                tienda_e = st.selectbox("Tienda", tiendas_opciones, index=tienda_idx, key=f"etienda_{d['id']}")
                            else:
                                st.warning(f"Este cliente no tiene tiendas cargadas — se mantiene: {d['tienda']}")
                                tienda_e = d["tienda"]
                            e1, e2, e3 = st.columns(3)
                            with e1:
                                roles_e = st.number_input("Roles", min_value=0, step=1, value=d["roles"], key=f"eroles_{d['id']}")
                            with e2:
                                tarimas_e = st.number_input("Tarimas", min_value=0, step=1, value=d["tarimas"], key=f"etarimas_{d['id']}")
                            with e3:
                                cajas_e = st.number_input("Bultos", min_value=0, step=1, value=d["cajas"], key=f"ecajas_{d['id']}")
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
                            # Ya no se elige — se deriva de la tienda seleccionada arriba,
                            # igual que en Despacho (es un dato fijo del catálogo).
                            tipo_pago_e = tiendas_cliente_g.get(tienda_e, {}).get("clasificacion", "Local")
                            st.text_input("Clasificación de Destino", value=tipo_pago_e, disabled=True, key=f"etipopago_{d['id']}")
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

                        st.markdown("##### CIERRE DEL VIAJE")
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
                                                       auxiliar_edit, destinos_editados, marchamo_regreso_edit.strip(),
                                                       usuario_activo)
                                if ok:
                                    st.success(f"Viaje {viaje_g['id_viaje']} corregido.")
                                    del st.session_state["viaje_gestion"]
                                    del st.session_state["destinos_gestion"]
                                    st.session_state.pop("resultados_gestion", None)
                                    st.rerun()
                                else:
                                    mostrar_resultado_error(msg, perfil_activo)
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
                                mostrar_resultado_error(msg, perfil_activo)
    else:
        st.info("Solo el perfil Administrador puede editar o anular viajes.")

# ==========================================
# MÓDULO 3: REPORTES (pendiente de construir)
# ==========================================
with tab3:
    reporte_sel = st.selectbox(
        "Reporte", ["Bitácora de Viajes", "Resumen de Liquidaciones", "Control de Retornable",
                    "Plan de Carga del Día (para el Dashboard)", "Bultos por Camión (próximamente)"]
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
                        data=exportar_csv(df_bitacora),
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
                        data=exportar_csv(df_mostrar),
                        file_name=f"liquidaciones_{fecha_ini_l}_a_{fecha_fin_l}.csv",
                        mime="text/csv",
                        use_container_width=True, key="csv_liq_rep"
                    )
        else:
            st.info("Elige el rango de fechas y el cliente, y presiona Generar.")

    elif reporte_sel == "Control de Retornable":
        st.subheader(":material/inventory_2: Control de Retornable")
        st.caption("Roles, Tarimas y Pacas de Cartón (reportado en Lbs, 50 lbs por paca) — las cajas no son "
                   "retornables, por eso no aparecen aquí. Mientras un viaje esté Pendiente de Liquidar, su "
                   "material se cuenta como 'todavía en tienda'.")

        rcol1, rcol2, rcol3, rcol4 = st.columns([1, 1, 1, 1])
        with rcol1:
            fecha_ini_r = st.date_input("Desde", value=ahora().date() - timedelta(days=30), key="fecha_ini_ret_rep")
        with rcol2:
            fecha_fin_r = st.date_input("Hasta", value=ahora().date(), key="fecha_fin_ret_rep")
        with rcol3:
            clientes_reporte_r = ["Todos"] + clientes_permitidos_para(usuario_activo, perfil_activo)
            cliente_reporte_r = st.selectbox("Cliente", clientes_reporte_r, key="cliente_ret_rep")
        with rcol4:
            material_sel = st.selectbox("Material", ["Todos", "Roles", "Tarimas", "Pacas de Cartón (Lbs)"], key="material_ret_rep")

        generar_r = st.button(":material/search: Generar", key="btn_generar_ret_rep")
        if generar_r:
            st.session_state["df_retornable_fecha"] = obtener_reporte_retornable_por_fecha(fecha_ini_r, fecha_fin_r, cliente_reporte_r)

        df_fecha = st.session_state.get("df_retornable_fecha")
        if df_fecha is not None:
            if df_fecha.empty:
                st.info("No hay movimientos en ese rango de fechas para ese cliente.")
            else:
                kcol1, kcol2, kcol3 = st.columns(3)
                if material_sel in ("Todos", "Roles"):
                    saldo_roles = int(df_fecha["Roles Enviados"].sum() - df_fecha["Roles Retornados"].sum())
                    kcol1.metric("Total Roles en Tiendas", saldo_roles)
                if material_sel in ("Todos", "Tarimas"):
                    saldo_tarimas = int(df_fecha["Tarimas Enviadas"].sum() - df_fecha["Tarimas Retornadas"].sum())
                    kcol2.metric("Total Tarimas en Tiendas", saldo_tarimas)
                if material_sel in ("Todos", "Pacas de Cartón (Lbs)"):
                    total_lbs = int(df_fecha["Lbs de Cartón"].sum())
                    kcol3.metric("Total Lbs de Cartón Retornadas", f"{total_lbs:,} lbs",
                                 help=f"{int(df_fecha['Pacas de Cartón Retornadas'].sum())} pacas × {LBS_POR_PACA_CARTON} lbs c/u")

                st.markdown("---")
                st.caption("Cada fila es la fecha en que salió el viaje: lo enviado ese día, y lo retornado "
                           "de ese mismo viaje (el retorno solo tiene valor una vez que ya se liquidó).")
                columnas_fecha_por_material = {
                    "Todos": ["Fecha", "Cliente", "Tienda", "Roles Enviados", "Roles Retornados",
                              "Tarimas Enviadas", "Tarimas Retornadas", "Pacas de Cartón Retornadas", "Lbs de Cartón"],
                    "Roles": ["Fecha", "Cliente", "Tienda", "Roles Enviados", "Roles Retornados"],
                    "Tarimas": ["Fecha", "Cliente", "Tienda", "Tarimas Enviadas", "Tarimas Retornadas"],
                    "Pacas de Cartón (Lbs)": ["Fecha", "Cliente", "Tienda", "Pacas de Cartón Retornadas", "Lbs de Cartón"],
                }
                df_fecha_mostrar = df_fecha[columnas_fecha_por_material[material_sel]]
                st.dataframe(df_fecha_mostrar, use_container_width=True, height=420)

                fcol1, fcol2 = st.columns(2)
                with fcol1:
                    st.download_button(
                        ":material/download: Exportar a Excel",
                        data=exportar_excel(df_fecha_mostrar),
                        file_name=f"retornable_detalle_{fecha_ini_r}_a_{fecha_fin_r}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True, key="excel_ret_rep_fecha"
                    )
                with fcol2:
                    st.download_button(
                        ":material/download: Exportar a CSV",
                        data=exportar_csv(df_fecha_mostrar),
                        file_name=f"retornable_detalle_{fecha_ini_r}_a_{fecha_fin_r}.csv",
                        mime="text/csv",
                        use_container_width=True, key="csv_ret_rep_fecha"
                    )
        else:
            st.info("Elige el rango de fechas, cliente y material, y presiona Generar.")

    elif reporte_sel == "Plan de Carga del Día (para el Dashboard)":
        st.subheader(":material/event_note: Plan de Carga del Día")
        st.caption("Esta es la meta (camiones y bultos por hora) contra la que el Dashboard de indicadores "
                   "compara lo que realmente se va cargando — 'Estatus de Carga de Camiones'. No afecta "
                   "nada dentro de esta app, solo alimenta ese dashboard aparte.")
        if perfil_activo not in ["Administrador", "SuperAdministrador", "Supervisor"]:
            st.info("Solo Administrador, SuperAdministrador y Supervisor pueden cargar el plan del día.")
        else:
            fecha_plan = st.date_input("Fecha del plan", value=ahora().date(), key="fecha_plan_carga")
            plan_actual = obtener_plan_carga(fecha_plan)
            df_plan = pd.DataFrame(plan_actual)
            st.caption("Edita directo en la tabla — una fila por hora del día.")
            df_plan_editado = st.data_editor(
                df_plan, use_container_width=True, height=460, hide_index=True,
                disabled=["Hora"], key=f"editor_plan_{fecha_plan}"
            )
            if st.button(":material/save: Guardar Plan del Día", key="btn_guardar_plan"):
                filas_guardar = df_plan_editado.to_dict("records")
                ok, msg = guardar_plan_carga(fecha_plan, filas_guardar, usuario_activo)
                if ok:
                    st.success(f"✅ Plan de carga del {fecha_plan} guardado.")
                else:
                    mostrar_resultado_error(msg, perfil_activo)
    else:
        st.info("Este reporte todavía no está construido — lo armamos en la próxima ronda.")

# ==========================================
# MÓDULO 4: CATÁLOGOS — descargar plantilla, llenar en Excel, subir para
# reemplazar el catálogo completo. Solo Administrador.
# ==========================================
with tab4:
    if perfil_activo not in ["Administrador", "SuperAdministrador", "Supervisor"]:
        st.info("Tu perfil no tiene acceso a la gestión de catálogos.")
    else:
        st.header(":material/settings: Gestión de Catálogos")

        # Mensaje de resultado de la última acción — se guarda antes del rerun()
        # para que sobreviva a la recarga y de verdad se alcance a leer, en vez
        # de aparecer y desaparecer en el mismo instante.
        flash = st.session_state.pop("flash_catalogos", None)
        if flash:
            tipo, mensaje = flash
            getattr(st, tipo)(mensaje)

        if perfil_activo in ("Administrador", "SuperAdministrador"):
            catalogos_disponibles = list(CATALOGOS_CONFIG.keys())
            mis_clientes = None  # sin restricción
            st.caption("Descarga la plantilla, llénala en Excel y súbela para actualizar ese catálogo.")
        else:
            # El Supervisor administra Camiones también — pero OJO: Camiones no
            # tiene una columna "cliente" en la base de datos (un camión no
            # pertenece a un solo cliente, se asigna viaje por viaje), así que
            # ese catálogo específico NO se puede filtrar por cliente todavía.
            # Un Supervisor ve/edita TODOS los camiones, no solo "los suyos".
            catalogos_disponibles = ["Clientes y Tiendas", "CDs por Cliente", "Camiones", "Acceso Usuario → Cliente"]
            mis_clientes = clientes_permitidos_para(usuario_activo, perfil_activo)
            st.caption(f"Ves y editas Clientes/Tiendas/CDs solo de: **{', '.join(mis_clientes) or '(ningún cliente asignado)'}**. "
                       "El catálogo de Camiones es compartido entre todos los clientes (no se puede filtrar por "
                       "cliente todavía), así que ahí ves la flota completa.")
            if not mis_clientes:
                st.warning("🚫 Todavía no tienes ningún cliente asignado — pídele a un Administrador que te dé acceso "
                           "desde la pestaña de Usuarios.")
                st.stop()

        # Camiones no tiene columna "cliente" en la base de datos — las funciones
        # de guardado ya lo detectan solas y no aplican ningún filtro en ese caso.
        catalogo_sel = st.selectbox("Catálogo a gestionar", catalogos_disponibles)
        config = CATALOGOS_CONFIG[catalogo_sel]
        columnas_mostrar = config["columnas"] + config.get("solo_lectura", [])
        df_actual_completo = leer_catalogo_actual(config["tabla"], columnas_mostrar)
        if mis_clientes is not None and "cliente" in config["columnas"]:
            df_actual_completo = df_actual_completo[df_actual_completo["cliente"].isin(mis_clientes)]
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
                ok, msg = sincronizar_catalogo(config["tabla"], config["columnas"], config["clave"], df_editado[config["columnas"]], usuario_activo, mis_clientes, permitir_borrado=True)
                if ok:
                    texto = "✅ Tabla actualizada correctamente."
                    if msg != "OK":
                        texto += f"\n\n⚠️ {msg}"
                    st.session_state["flash_catalogos"] = ("success", texto)
                    st.session_state.catalogos = cargar_catalogos_desde_db()
                    st.rerun()
                else:
                    mostrar_resultado_error(msg, perfil_activo)
        st.caption(f"{len(df_actual)} registro(s) actualmente.")
        st.download_button(
            ":material/download: Descargar datos actuales (Excel)",
            data=exportar_excel(df_actual_completo),
            file_name=f"{config['tabla']}_actual.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"descargar_actual_{catalogo_sel}"
        )

        st.markdown("#### ➕ Agregar o corregir un registro")
        st.caption("Para un cambio puntual, sin tener que subir un Excel completo. Si la llave "
                   "ya existe, se actualiza en vez de duplicarse.")

        NUEVO_CLIENTE_OPCION = "➕ Nuevo cliente..."
        valores_form = {}
        for col in config["columnas"]:
            if col == "cliente":
                # Selector en vez de texto libre: evita que cada persona escriba el
                # mismo cliente con variaciones distintas (typos, mayúsculas, espacios).
                # Si hay alcance limitado (Supervisor), solo puede
                # elegir entre SUS clientes, y no puede crear uno nuevo.
                if mis_clientes is not None:
                    clientes_existentes = sorted(mis_clientes)
                    valores_form["cliente"] = st.selectbox("Cliente", clientes_existentes, key=f"campo_{catalogo_sel}_cliente_sel")
                else:
                    clientes_existentes = sorted(st.session_state.catalogos["clientes_lista_activos"])
                    cliente_elegido = st.selectbox(
                        "Cliente", clientes_existentes + [NUEVO_CLIENTE_OPCION], key=f"campo_{catalogo_sel}_cliente_sel"
                    )
                    if cliente_elegido == NUEVO_CLIENTE_OPCION:
                        valores_form["cliente"] = st.text_input("Nombre del cliente nuevo", key=f"campo_{catalogo_sel}_cliente_nuevo")
                    else:
                        valores_form["cliente"] = cliente_elegido
            elif col == "usuario" and catalogo_sel != "Usuarios":
                # En "Acceso Usuario → Cliente" el usuario debe ser uno que ya
                # exista en el catálogo de Usuarios, no texto libre. Si quien
                # gestiona tiene alcance limitado, solo puede dar acceso a cuentas
                # Operador/Liquidador — nunca a otro Supervisor,
                # Supervisor, o Administrador.
                if mis_clientes is not None:
                    usuarios_existentes = sorted(
                        u for u, p in st.session_state.catalogos["usuarios"].items() if p in ("Operador", "Liquidador")
                    )
                else:
                    usuarios_existentes = sorted(st.session_state.catalogos["usuarios"].keys())
                if usuarios_existentes:
                    valores_form["usuario"] = st.selectbox("Usuario", usuarios_existentes, key=f"campo_{catalogo_sel}_usuario_sel")
                else:
                    st.warning("No hay usuarios disponibles para asignar todavía — créalos primero en la pestaña Usuarios.")
                    valores_form["usuario"] = ""
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
            elif col == "clasificacion":
                valores_form["clasificacion"] = st.selectbox("Clasificación (Local/Departamental)", ["Local", "Departamental"], key=f"campo_{catalogo_sel}_clasificacion_sel")
            elif col in config.get("booleanas", []):
                valores_form[col] = st.checkbox("Activo", value=True, key=f"campo_{catalogo_sel}_{col}",
                                                  help="Desmárcalo para que ya no aparezca en los menús de Despacho, sin borrar su historial.")
            elif col in config["numericas"]:
                valores_form[col] = st.number_input(col.replace("_", " ").title(), min_value=0.0, step=1.0, key=f"campo_{catalogo_sel}_{col}")
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
                # (Solo aplica cuando NO hay alcance limitado — un Supervisor nunca
                # puede crear un cliente nuevo por su cuenta.)
                if "cliente" in valores_form and catalogo_sel != "Clientes" and mis_clientes is None:
                    agregar_o_actualizar_registro("cat_clientes", ["nombre"], ["nombre"], {"nombre": valores_form["cliente"]}, usuario_activo)
                ok, msg = agregar_o_actualizar_registro(config["tabla"], config["columnas"], config["clave"], valores_form, usuario_activo, mis_clientes)
                if ok:
                    st.session_state["flash_catalogos"] = ("success", "✅ Registro guardado correctamente.")
                    st.session_state.catalogos = cargar_catalogos_desde_db()
                    st.rerun()
                else:
                    mostrar_resultado_error(msg, perfil_activo)

        if not df_actual.empty:
            st.markdown("#### 🗑️ Eliminar un registro")
            opciones_borrar = df_actual.apply(lambda r: " | ".join(str(r[c]) for c in config["clave"]), axis=1).tolist()
            registro_borrar = st.selectbox("Selecciona el registro a eliminar", opciones_borrar, key=f"del_sel_{catalogo_sel}")
            valores_clave = dict(zip(config["clave"], registro_borrar.split(" | ")))
            bcol1, bcol2 = st.columns([1, 1.4])
            with bcol1:
                if st.button(":material/delete: Eliminar Registro Seleccionado", key=f"del_btn_{catalogo_sel}"):
                    ok, msg = eliminar_registro(config["tabla"], config["clave"], valores_clave, usuario_activo, mis_clientes)
                    if ok:
                        st.session_state["flash_catalogos"] = ("success", "✅ Registro eliminado correctamente.")
                        st.session_state.catalogos = cargar_catalogos_desde_db()
                        st.rerun()
                    else:
                        mostrar_resultado_error(msg, perfil_activo)
            with bcol2:
                if st.button(":material/search: Ver qué está usando esto", key=f"del_uso_{catalogo_sel}"):
                    uso = verificar_uso(config["tabla"], valores_clave)
                    if not uso:
                        st.success("✅ Nada lo está usando — se puede eliminar sin problema.")
                    else:
                        nombres_amigables = {
                            "cat_camiones": "camión(es)", "viajes": "viaje(s)",
                            "cat_clientes_tiendas": "tienda(s)", "cat_cds_por_cliente": "CD(s) asignado(s)",
                            "cat_usuario_clientes": "acceso(s) de usuario",
                        }
                        detalle = ", ".join(f"{cantidad} {nombres_amigables.get(t, t)}" for t, cantidad in uso)
                        st.warning(f"⚠️ No se puede eliminar todavía — está en uso en: {detalle}. "
                                   f"Si ya no corresponde, considera marcarlo como **Inactivo** en vez de borrarlo, "
                                   f"para no perder ese historial.")

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
                    st.info(f"Esto agrega los registros nuevos y actualiza los que ya existan (por su llave) — "
                            f"nunca borra nada. '{catalogo_sel}': {len(df_actual)} registro(s) actuales, "
                            f"{len(df_nuevo)} en el archivo.")
                    if st.button("🔄 Actualizar Catálogo"):
                        ok, msg = sincronizar_catalogo(config["tabla"], config["columnas"], config["clave"], df_nuevo, usuario_activo, mis_clientes)
                        if ok:
                            texto = f"✅ Carga masiva completada: '{catalogo_sel}' actualizado con {len(df_nuevo)} registro(s)."
                            if msg != "OK":
                                texto += f"\n\n⚠️ {msg}"
                            st.session_state["flash_catalogos"] = ("success", texto)
                            st.session_state.catalogos = cargar_catalogos_desde_db()
                            st.rerun()
                        else:
                            st.session_state["flash_catalogos"] = ("error", f"❌ La carga masiva tuvo errores y no se aplicó ningún cambio: {msg}")
                            st.rerun()
            except Exception as e:
                mostrar_resultado_error(_error_tecnico(e, "leer_archivo_carga_masiva"), perfil_activo)

        # ---- Borrado Masivo — SOLO SuperAdministrador. A propósito, "Administrador"
        # NO tiene esta herramienta — es la única acción de la app que puede tumbar
        # datos en bloque sin poder revisarlos uno por uno antes de confirmar. ----
        if perfil_activo == "SuperAdministrador":
            st.markdown("---")
            with st.expander("🗑️ Zona de Riesgo — Borrado Masivo (solo SuperAdministrador)"):
                st.error("Esto borra de verdad, en bloque, y no se puede deshacer. Úsalo solo para limpiar "
                         "datos de prueba o un cliente que ya no corresponde.")
                catalogo_borrar = st.selectbox("Catálogo", list(CATALOGOS_CONFIG.keys()), key="catalogo_borrar_masivo")
                config_borrar = CATALOGOS_CONFIG[catalogo_borrar]
                tiene_cliente = "cliente" in config_borrar["columnas"]

                if tiene_cliente:
                    clientes_en_tabla = sorted(leer_catalogo_actual(config_borrar["tabla"], ["cliente"])["cliente"].unique().tolist())
                    alcance_opciones = clientes_en_tabla + ["TODO EL CATÁLOGO"]
                else:
                    alcance_opciones = ["TODO EL CATÁLOGO"]
                alcance_sel = st.selectbox("Alcance", alcance_opciones, key="alcance_borrar_masivo")

                cantidad = contar_borrado_masivo(config_borrar["tabla"], "cliente" if tiene_cliente else None, alcance_sel)
                if cantidad == 0:
                    st.caption("No hay registros que coincidan — nada que borrar.")
                else:
                    st.warning(f"⚠️ Esto va a borrar **{cantidad} registro(s)** de '{catalogo_borrar}'"
                               f"{' del cliente ' + alcance_sel if alcance_sel != 'TODO EL CATÁLOGO' else ' — EL CATÁLOGO COMPLETO'}.")
                    frase_esperada = f"BORRAR {catalogo_borrar.upper()}"
                    confirmacion = st.text_input(f"Escribe exactamente: {frase_esperada}", key="confirmacion_borrar_masivo")
                    if st.button(":material/delete_forever: Borrar Definitivamente", disabled=(confirmacion.strip() != frase_esperada)):
                        ok, resultado = borrar_masivo(config_borrar["tabla"], "cliente" if tiene_cliente else None, alcance_sel, usuario_activo)
                        if ok:
                            st.session_state["flash_catalogos"] = ("success", f"✅ Borrado masivo completado: se borraron {resultado} registro(s) de '{catalogo_borrar}'.")
                            st.session_state.catalogos = cargar_catalogos_desde_db()
                            st.rerun()
                        else:
                            st.session_state["flash_catalogos"] = ("error", f"❌ El borrado masivo falló, no se borró nada (probablemente algo ahí está en uso en Camiones o Viajes): {resultado}")
                            st.rerun()

# ==========================================
# MÓDULO 6: GESTIÓN DE USUARIOS — SuperAdministrador ve y administra a todos;
# Administrador administra Operador/Liquidador/Supervisor (no puede tocar
# cuentas de su mismo nivel o superior); Supervisor solo ve/administra cuentas
# Operador y Liquidador.
# ==========================================
with tab6:
    if perfil_activo not in ["Administrador", "SuperAdministrador", "Supervisor"]:
        st.info("Tu perfil no tiene acceso a la gestión de usuarios.")
    else:
        st.header(":material/manage_accounts: Gestión de Usuarios")

        if perfil_activo == "SuperAdministrador":
            perfiles_visibles = None  # ve todos
            perfiles_asignables = ["SuperAdministrador", "Administrador", "Operador", "Liquidador", "Supervisor"]
            st.caption("Ves y administras todas las cuentas.")
        elif perfil_activo == "Administrador":
            perfiles_visibles = ["Operador", "Liquidador", "Supervisor"]
            perfiles_asignables = ["Operador", "Liquidador", "Supervisor"]
            st.caption("Ves y administras cuentas Operador, Liquidador y Supervisor — no puedes crear ni ver "
                       "cuentas Administrador o SuperAdministrador.")
        else:
            perfiles_visibles = ["Operador", "Liquidador"]
            perfiles_asignables = ["Operador", "Liquidador"]
            st.caption("Como Supervisor, solo ves y administras cuentas Operador y Liquidador.")

        st.markdown("#### Usuarios actuales")
        usuarios_lista = listar_usuarios_gestion(perfiles_visibles)
        if usuarios_lista:
            df_usuarios = pd.DataFrame(usuarios_lista)
            df_usuarios["activo"] = df_usuarios["activo"].map({True: "✅ Activo", False: "🚫 Desactivado"})
            df_usuarios["debe_cambiar_password"] = df_usuarios["debe_cambiar_password"].map({True: "Sí", False: "No"})
            df_usuarios.columns = ["Usuario", "Perfil", "Estado", "Debe Cambiar Contraseña", "Creado Por"]
            st.dataframe(df_usuarios, use_container_width=True, height=250)
        else:
            st.caption("No hay usuarios para mostrar.")

        st.markdown("---")
        st.markdown("#### :material/person_add: Crear usuario nuevo")
        nc1, nc2 = st.columns(2)
        with nc1:
            nuevo_usuario = st.text_input("Nombre de usuario", key="nuevo_usuario_gestion")
        with nc2:
            nuevo_perfil = st.selectbox("Perfil", perfiles_asignables, key="nuevo_perfil_gestion")

        rol_ve_todos_los_clientes = perfil_activo in ("Administrador", "SuperAdministrador")
        clientes_para_ofrecer = sorted(st.session_state.catalogos["clientes_lista_activos"]) if rol_ve_todos_los_clientes \
            else sorted(clientes_permitidos_para(usuario_activo, perfil_activo))
        nuevo_perfil_ve_todos = nuevo_perfil in ("Administrador", "SuperAdministrador")
        if not nuevo_perfil_ve_todos:
            st.caption("Los clientes se asignan aquí mismo — un usuario sin ningún cliente asignado se queda "
                       "atorado al entrar, así que ya no se puede crear uno sin elegir al menos uno.")
            nuevos_clientes = st.multiselect("Clientes con acceso", clientes_para_ofrecer, key="nuevos_clientes_gestion")
        else:
            nuevos_clientes = []  # Administrador/SuperAdministrador ven todos los clientes automáticamente

        if st.button(":material/person_add: Crear Usuario", key="btn_crear_usuario"):
            if not nuevo_usuario.strip():
                st.warning("Escribe un nombre de usuario.")
            elif not nuevo_perfil_ve_todos and not nuevos_clientes:
                st.error("❌ Debes asignarle al menos un cliente — si no, el usuario queda sin poder entrar a trabajar.")
            else:
                ok, resultado = crear_usuario(nuevo_usuario.strip(), nuevo_perfil, usuario_activo, nuevos_clientes)
                if ok:
                    st.success(f"✅ Usuario '{nuevo_usuario.strip()}' creado, con acceso a: {', '.join(nuevos_clientes) or 'todos los clientes'}.")
                    st.info(f"🔑 Contraseña temporal (cópiala y pásasela — no se vuelve a mostrar): **{resultado}**")
                    st.caption("Quedará forzado a cambiarla en su primer ingreso.")
                    st.session_state.catalogos = cargar_catalogos_desde_db()
                else:
                    mostrar_resultado_error(resultado, perfil_activo)

        st.markdown("---")
        st.markdown("#### :material/key: Restablecer contraseña")
        usuarios_gestionables = [u["usuario"] for u in usuarios_lista]
        if usuarios_gestionables:
            usuario_reset = st.selectbox("Usuario", usuarios_gestionables, key="usuario_reset_gestion")
            if st.button(":material/key: Generar Nueva Contraseña Temporal", key="btn_reset_password"):
                password_temp = generar_password_temporal()
                ok, msg = establecer_password(usuario_reset, password_temp, forzar_cambio_siguiente=True, cambiado_por=usuario_activo)
                if ok:
                    st.success(f"✅ Contraseña restablecida para '{usuario_reset}'.")
                    st.info(f"🔑 Contraseña temporal (cópiala y pásasela): **{password_temp}**")
                    st.caption("Quedará forzado a cambiarla en su próximo ingreso.")
                else:
                    mostrar_resultado_error(msg, perfil_activo)
        else:
            st.caption("No hay usuarios disponibles para restablecer.")

        st.markdown("---")
        st.markdown("#### :material/toggle_on: Activar / Desactivar cuenta")
        if usuarios_gestionables:
            usuario_toggle = st.selectbox("Usuario", usuarios_gestionables, key="usuario_toggle_gestion")
            estado_actual = next((u["activo"] for u in usuarios_lista if u["usuario"] == usuario_toggle), True)
            tc1, tc2 = st.columns(2)
            with tc1:
                if estado_actual and st.button(":material/block: Desactivar Cuenta", key="btn_desactivar"):
                    ok, msg = cambiar_estado_usuario(usuario_toggle, False, usuario_activo)
                    if ok:
                        st.success(f"Cuenta de '{usuario_toggle}' desactivada.")
                        st.session_state.catalogos = cargar_catalogos_desde_db()
                        st.rerun()
                    else:
                        mostrar_resultado_error(msg, perfil_activo)
            with tc2:
                if not estado_actual and st.button(":material/check_circle: Reactivar Cuenta", key="btn_reactivar"):
                    ok, msg = cambiar_estado_usuario(usuario_toggle, True, usuario_activo)
                    if ok:
                        st.success(f"Cuenta de '{usuario_toggle}' reactivada.")
                        st.session_state.catalogos = cargar_catalogos_desde_db()
                        st.rerun()
                    else:
                        mostrar_resultado_error(msg, perfil_activo)

        if perfil_activo in ("Administrador", "SuperAdministrador"):
            st.markdown("---")
            st.markdown("#### :material/history: Bitácora de Auditoría")
            st.caption("Quién hizo qué y cuándo — viajes creados/editados/anulados/liquidados, cambios en "
                       "catálogos, y eventos de usuarios (incluye intentos de login fallidos).")
            acol1, acol2, acol3, acol4 = st.columns([1, 1, 1.3, 0.7])
            with acol1:
                fecha_ini_aud = st.date_input("Desde", value=ahora().date() - timedelta(days=7), key="fecha_ini_aud")
            with acol2:
                fecha_fin_aud = st.date_input("Hasta", value=ahora().date(), key="fecha_fin_aud")
            with acol3:
                usuarios_aud = ["Todos"] + sorted(st.session_state.catalogos["usuarios"].keys())
                usuario_filtro_aud = st.selectbox("Usuario", usuarios_aud, key="usuario_filtro_aud")
            with acol4:
                st.write("")
                generar_aud = st.button(":material/search: Ver", use_container_width=True, key="btn_ver_auditoria")

            if generar_aud:
                st.session_state["df_auditoria"] = obtener_auditoria(fecha_ini_aud, fecha_fin_aud, usuario_filtro_aud)

            df_aud = st.session_state.get("df_auditoria")
            if df_aud is not None:
                if df_aud.empty:
                    st.info("No hay actividad registrada en ese rango de fechas.")
                else:
                    st.dataframe(df_aud, use_container_width=True, height=380)
                    acol_e1, acol_e2 = st.columns(2)
                    with acol_e1:
                        st.download_button(
                            ":material/download: Exportar a Excel",
                            data=exportar_excel(df_aud),
                            file_name=f"auditoria_{fecha_ini_aud}_a_{fecha_fin_aud}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True, key="excel_aud"
                        )
                    with acol_e2:
                        st.download_button(
                            ":material/download: Exportar a CSV",
                            data=exportar_csv(df_aud),
                            file_name=f"auditoria_{fecha_ini_aud}_a_{fecha_fin_aud}.csv",
                            mime="text/csv",
                            use_container_width=True, key="csv_aud"
                        )
            else:
                st.info("Elige el rango de fechas y presiona 'Ver'.")

# ==========================================
# PIE DE PÁGINA DE LA HERRAMIENTA (visible en toda la app)
# ==========================================
st.markdown("---")
st.markdown(
    "<div style='text-align:center; color:#5B6169; font-size:12px; padding:8px 0;'>"
    f"Ransa · Sistema de Control de Ruta · v{VERSION_APP} · Ideado por Ángel Arriaza"
    "</div>",
    unsafe_allow_html=True
)
