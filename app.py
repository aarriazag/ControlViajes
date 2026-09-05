import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import psycopg2
import psycopg2.extras
import json
from datetime import datetime
from contextlib import closing

# Configuración de la página web con estilo e identidad corporativa
st.set_page_config(page_title="Ransa - Gestión Logística de Viajes", layout="wide", page_icon="🚚")

# --- ESTILOS VISUALES RANSA (VERDE Y BLANCO) ---
st.markdown("""
    <style>
        div.stButton > button:first-child {
            background-color: #007A33 !important;
            color: white !important;
            border-radius: 6px !important;
            border: none !important;
            font-weight: bold !important;
        }
        div.stButton > button:first-child:hover {
            background-color: #005C25 !important;
            color: white !important;
        }
        button[data-baseweb="tab"] {
            font-size: 16px !important;
            font-weight: bold !important;
        }
        button[aria-selected="true"] {
            color: #007A33 !important;
            border-bottom-color: #007A33 !important;
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
        conn.commit()


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
                fecha_hoy = datetime.now().strftime("%Y-%m-%d")
                hora_hoy = datetime.now().strftime("%H:%M:%S")

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
                        "devolucion, creditos, pg_cajas, es_complemento) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (viaje_id, i + 1, dest["tienda"], dest["km"], dest["galones_base"], dest["pedidos"],
                         dest["marchamo_ida"], dest["marchamo_regreso"] or None,
                         dest["roles"], dest["tarimas"], dest["cajas"],
                         dest.get("remitos", ""), dest.get("incidencias", ""),
                         dest.get("devolucion", ""), dest.get("creditos", ""),
                         dest.get("pg_cajas", 0), dest.get("es_complemento", False))
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


def buscar_viaje(valor_busqueda):
    """Busca un viaje por No. de Viaje, Marchamo de Ida (de cualquiera de sus destinos), o Placa.
    Devuelve (viaje, destinos) o (None, []) si no encuentra nada."""
    valor = valor_busqueda.strip()
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT v.* FROM viajes v
            WHERE v.id_viaje = %s OR v.placa = %s
               OR EXISTS (SELECT 1 FROM destinos d WHERE d.viaje_id = v.id AND d.marchamo_ida = %s)
            ORDER BY v.id DESC LIMIT 1
        """, (valor, valor, valor))
        viaje = cur.fetchone()
        if not viaje:
            return None, []
        cur.execute("SELECT * FROM destinos WHERE viaje_id = %s ORDER BY orden", (viaje["id"],))
        destinos = cur.fetchall()
        return viaje, destinos


def anular_viaje(viaje_id, usuario, motivo, liberar_marchamos=True):
    """Marca el viaje como Anulado (no lo borra, queda como registro para auditoría).
    Por defecto libera los marchamos de sus destinos para que puedan reutilizarse en
    otro viaje, ya que un viaje anulado normalmente significa un error de digitación,
    no un marchamo físicamente gastado. Esto es ajustable con liberar_marchamos=False."""
    with closing(get_conn()) as conn:
        try:
            with conn.cursor() as cur:
                fecha_hoy = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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


def generar_hoja_control_html(viaje, destinos):
    """Arma la Hoja de Control de Viaje como HTML: en pantalla se ve con los colores
    de Ransa, pero al imprimir (@media print) los fondos de color se vuelven blancos
    y solo quedan bordes negros, para que salga limpia en una impresora blanco y negro."""
    marchamo_ida_general = destinos[0]["marchamo_ida"] if destinos else ""

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
            f'<div class="material-box"><b>{d["pg_cajas"] or 0}</b><span>CAJAS P&amp;G</span></div>'
            if not d["es_complemento"] else ""
        )

        bloques_destino += f"""
        <div class="destino-card">
            <div class="destino-header">
                <span class="destino-num">{idx}</span> {d['tienda']}
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
                    <div class="sub-info">No. de Pedidos: <b>{pedidos_txt}</b></div>
                    <div class="sub-info">Remisión: <b>{d['remitos'] or '—'}</b> &nbsp;|&nbsp;
                        Devolución: <b>{d['devolucion'] or '—'}</b> &nbsp;|&nbsp;
                        Créditos: <b>{d['creditos'] or '—'}</b></div>
                    {incidencia_html}
                </div>
                <div class="material-devuelto">
                    <div class="etiqueta">DEVUELTO POR LA TIENDA (LLENAR A MANO)</div>
                    <div class="material-grid">
                        <div class="material-box vacio"><span>ROLES</span></div>
                        <div class="material-box vacio"><span>TARIMAS</span></div>
                        <div class="material-box vacio"><span>PACAS CARTÓN</span></div>
                    </div>
                    <div class="firmas">
                        <div class="firma">Recibe (tienda)</div>
                        <div class="firma">Piloto / Auxiliar</div>
                    </div>
                </div>
            </div>
        </div>
        """

    return f"""
    <html>
    <head>
    <style>
        body {{ font-family: Arial, sans-serif; color: #1a1a1a; padding: 20px; }}
        .hoja {{ max-width: 800px; margin: auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: flex-start;
                   border-bottom: 4px solid #007A33; padding-bottom: 10px; margin-bottom: 15px; }}
        .logo {{ font-size: 28px; font-weight: bold; color: #007A33; }}
        .titulo {{ text-align: right; }}
        .titulo h2 {{ margin: 0; color: #007A33; }}
        .titulo p {{ margin: 0; color: #666; font-size: 12px; }}
        .datos-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px 20px; margin-bottom: 20px; }}
        .dato label {{ display: block; font-size: 11px; color: #666; text-transform: uppercase; }}
        .dato span {{ font-size: 15px; font-weight: bold; border-bottom: 1px solid #ccc; display: block; }}
        .titulo-destinos {{ background: #007A33; color: white; padding: 6px 12px; font-weight: bold;
                             border-radius: 4px; margin-bottom: 10px; }}
        .destino-card {{ border: 1px solid #ccc; border-radius: 6px; margin-bottom: 15px; overflow: hidden; }}
        .destino-header {{ background: #eef6ef; padding: 8px 12px; font-weight: bold; position: relative; }}
        .destino-num {{ background: #007A33; color: white; border-radius: 50%; padding: 2px 9px; margin-right: 6px; }}
        .badge-regreso {{ float: right; background: #E8804A; color: white; padding: 3px 10px;
                           border-radius: 4px; font-size: 12px; }}
        .badge-complemento {{ background: #A63603; color: white; padding: 3px 10px;
                               border-radius: 4px; font-size: 12px; margin-left: 8px; }}
        .destino-body {{ display: flex; }}
        .material-enviado, .material-devuelto {{ flex: 1; padding: 10px 12px; }}
        .material-devuelto {{ border-left: 2px dashed #ccc; }}
        .etiqueta {{ font-size: 11px; color: #007A33; font-weight: bold; margin-bottom: 6px; }}
        .material-grid {{ display: flex; gap: 8px; }}
        .material-box {{ flex: 1; border: 1px solid #ccc; border-radius: 4px; text-align: center; padding: 8px 4px; }}
        .material-box b {{ display: block; font-size: 18px; color: #007A33; }}
        .material-box.vacio {{ min-height: 40px; }}
        .material-box span {{ font-size: 10px; color: #666; }}
        .sub-info {{ font-size: 12px; margin-top: 6px; }}
        .incidencia {{ font-size: 12px; margin-top: 6px; color: #b34700; }}
        .firmas {{ display: flex; justify-content: space-between; margin-top: 25px; font-size: 11px; color: #666; }}
        .firma {{ border-top: 1px solid #666; padding-top: 3px; width: 45%; text-align: center; }}
        .footer {{ display: flex; justify-content: space-between; font-size: 11px; color: #999; margin-top: 20px; }}
        .btn-imprimir {{ background: #007A33; color: white; border: none; padding: 10px 20px;
                          border-radius: 6px; font-weight: bold; cursor: pointer; margin-bottom: 15px; }}

        @media print {{
            .btn-imprimir {{ display: none; }}
            .logo, .titulo h2, .dato span, .etiqueta, .destino-num, .material-box b {{ color: #000 !important; }}
            .titulo-destinos, .destino-header, .destino-num {{ background: #fff !important; border: 1px solid #000; color: #000 !important; }}
            .badge-regreso {{ background: #fff !important; color: #000 !important; border: 1px solid #000; }}
            .badge-complemento {{ background: #fff !important; color: #000 !important; border: 1px solid #000; }}
            .material-box {{ border: 1px solid #000; }}
        }}
    </style>
    </head>
    <body>
    <div class="hoja">
        <button class="btn-imprimir" onclick="window.print()">🖨️ Imprimir</button>
        <div class="header">
            <div class="logo">🚚 RANSA</div>
            <div class="titulo">
                <h2>HOJA DE SALIDA</h2>
                <p>Control de Ruta · Documento de Despacho</p>
            </div>
        </div>
        <div class="datos-grid">
            <div class="dato"><label>No. de Viaje</label><span>{viaje['id_viaje']}</span></div>
            <div class="dato"><label>Cliente</label><span>{viaje['cliente']}</span></div>
            <div class="dato"><label>CD Origen</label><span>{viaje['cd_origen'] or '—'}</span></div>
            <div class="dato"><label>Transportista</label><span>{viaje['transportista']}</span></div>
            <div class="dato"><label>Placa</label><span>{viaje['placa']}</span></div>
            <div class="dato"><label>Fecha</label><span>{viaje['fecha_creacion']}</span></div>
            <div class="dato"><label>Hora</label><span>{viaje['hora_creacion']}</span></div>
            <div class="dato"><label>Piloto</label><span>{viaje['piloto']}</span></div>
            <div class="dato"><label>Auxiliar</label><span>{viaje['auxiliar']}</span></div>
            <div class="dato"><label>Marchamo de Ida</label><span>{marchamo_ida_general}</span></div>
            <div class="dato"><label>Generado por</label><span>{viaje['usuario_creador']}</span></div>
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
                fecha_hoy = datetime.now().strftime("%Y-%m-%d")
                hora_hoy = datetime.now().strftime("%H:%M:%S")
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
# CATÁLOGOS MAESTROS (por ahora en memoria; candidatos a moverse a
# tablas propias más adelante si se necesita editarlos desde la app)
# ==========================================
if 'catalogos' not in st.session_state:
    st.session_state.catalogos = {
        "usuarios": {
            "Admin_Logistica": "Administrador",
            "Op_Salidas": "Operador",
            "Liq_Transporte": "Liquidador"
        },
        "transportistas": ["Transportes Express", "Logística del Norte", "Flota Interna"],
        # Valores de ejemplo — ajústalos a los reales cuando tengamos el módulo de catálogos
        "clientes": {
            "Dollarcity": {
                "Dollarcity Zona 10": {"km": 15.5, "galones_base": 3.5},
                "Dollarcity Mixco": {"km": 32.0, "galones_base": 7.0}
            },
            "UniSuper": {
                "UniSuper Central": {"km": 22.1, "galones_base": 5.0}
            },
            "UniSuper Importados": {
                "UniSuper Importados Norte": {"km": 18.0, "galones_base": 4.0}
            },
            "UniSuper LTX": {
                "UniSuper LTX Sur": {"km": 45.3, "galones_base": 10.0}
            }
        },
        # CDs posibles por cliente. Si un cliente solo tiene uno, se confirma
        # automático; si tiene varios, la app deja elegir cuál.
        "cds_por_cliente": {
            "Dollarcity": ["CD Barcenas", "CD Central"],
            "UniSuper": ["CD Barcenas"],
            "UniSuper Importados": ["CD Barcenas"],
            "UniSuper LTX": ["CD Barcenas"]
        },
        "camiones": {
            "C-123ABC": {"tipo": "5 Ton", "transportista": "Transportes Express", "piloto": "Juan Pérez", "auxiliar": "Carlos López"},
            "C-456DEF": {"tipo": "10 Ton", "transportista": "Logística del Norte", "piloto": "María Rodríguez", "auxiliar": "Pedro Gómez"},
            "C-789GHI": {"tipo": "20 Ton", "transportista": "Flota Interna", "piloto": "Luis Martínez", "auxiliar": "José Hernández"}
        },
        "pilotos": ["Juan Pérez", "María Rodríguez", "Luis Martínez", "Andrés Custodio"],
        "auxiliares": ["Carlos López", "Pedro Gómez", "José Hernández", "Ramiro Ruiz"]
    }

# Contador de "corridas" del formulario: se incrementa después de guardar un
# viaje para que los widgets nazcan con keys nuevas (así el formulario queda
# limpio para el siguiente viaje, en vez de arrastrar lo digitado antes).
if 'form_run' not in st.session_state:
    st.session_state.form_run = 0
if 'num_destinos' not in st.session_state:
    st.session_state.num_destinos = 1

# ==========================================
# SIDEBAR
# ==========================================
st.sidebar.markdown("<h2 style='color:#007A33; font-weight:bold;'>RANSA</h2>", unsafe_allow_html=True)
st.sidebar.title("🔐 Configuración Inicial")

st.sidebar.markdown("---")
try:
    with closing(get_conn()):
        pass
    st.sidebar.success("🟢 Conectado a la base de datos")
except Exception as e:
    st.sidebar.error(f"🔴 Sin conexión a la base de datos: {e}")
    st.stop()

# --- Bloqueo de sesión: Cliente y CD Origen se eligen UNA SOLA VEZ al entrar.
# Para cambiarlos hay que cerrar esta pestaña y volver a abrir la app (eso
# reinicia la sesión). Esto evita que a mitad de una jornada alguien cambie sin
# querer el cliente/CD y se mezclen viajes de otro contexto.
st.sidebar.markdown("---")
st.sidebar.subheader("🔒 Cliente y CD (fijo por sesión)")

if not st.session_state.get("config_bloqueada"):
    cliente_activo_sel = st.sidebar.selectbox("🎯 Cliente", list(st.session_state.catalogos["clientes"].keys()))

    cds_disponibles = st.session_state.catalogos["cds_por_cliente"].get(cliente_activo_sel, [])
    if len(cds_disponibles) <= 1:
        cd_origen_sel = cds_disponibles[0] if cds_disponibles else ""
        st.sidebar.info(f"CD Origen (único, automático): **{cd_origen_sel}**")
    else:
        cd_origen_sel = st.sidebar.selectbox("CD Origen", cds_disponibles)

    if st.sidebar.button("🔒 Confirmar y Comenzar a Trabajar"):
        st.session_state["cliente_activo_fijo"] = cliente_activo_sel
        st.session_state["cd_origen_fijo"] = cd_origen_sel
        st.session_state["config_bloqueada"] = True
        st.rerun()
    st.info("👈 Selecciona el Cliente (y el CD Origen si el cliente tiene más de uno) en la "
            "barra lateral, y confirma para comenzar a trabajar.")
    st.stop()

cliente_activo = st.session_state["cliente_activo_fijo"]
cd_origen_fijo = st.session_state["cd_origen_fijo"]

st.sidebar.success(f"Cliente: **{cliente_activo}**")
st.sidebar.success(f"CD Origen: **{cd_origen_fijo}**")
if st.sidebar.button("🚪 Cambiar Cliente / CD"):
    st.session_state["config_bloqueada"] = False
    del st.session_state["cliente_activo_fijo"]
    del st.session_state["cd_origen_fijo"]
    st.rerun()
st.sidebar.caption("⚠️ Si tienes un viaje a medio llenar sin guardar, se pierde al cambiar de cliente.")

st.sidebar.markdown("---")
usuario_activo = st.sidebar.selectbox("Simular Usuario Activo", list(st.session_state.catalogos["usuarios"].keys()))
perfil_activo = st.session_state.catalogos["usuarios"][usuario_activo]
st.sidebar.info(f"**Perfil:** {perfil_activo}")
st.sidebar.caption("⚠️ Esto es una simulación de usuario, no un login real. Falta agregar autenticación con contraseña antes de usarlo en producción con varios digitadores.")

st.sidebar.markdown("---")
st.sidebar.subheader("⛽ Parámetros Económicos")
precio_diesel_semana = st.sidebar.number_input("Precio Diésel por Galón ($)", min_value=1.0, value=4.50, step=0.10)

st.markdown("<h1 style='color: #007A33;'>🚚 Ransa · Sistema Integral de Gestión Logística (TMS)</h1>", unsafe_allow_html=True)
st.markdown(f"### Operando Cliente: <span style='color:#007A33;'>{cliente_activo}</span>", unsafe_allow_html=True)
st.markdown("---")

tab1, tab2, tab3 = st.tabs(["📋 Despacho (Salidas)", "💰 Recepción (Liquidaciones)", "📊 Reportes e Impacto Diésel"])

# ==========================================
# MÓDULO 1: DESPACHO / CREACIÓN DE VIAJES
# ==========================================
with tab1:
    if perfil_activo in ["Administrador", "Operador"]:
        st.header("Generación de Hoja de Control de Viaje")
        st.caption(f"👤 Digitando como: **{usuario_activo}** ({perfil_activo}) · CD Origen: **{cd_origen_fijo}**")

        run = st.session_state.form_run  # sufijo de las keys del formulario actual

        c1, c2, c3 = st.columns(3)
        c1.metric("No. Viaje (vista previa)", peek_siguiente_correlativo(cliente_activo))
        c2.text_input("Fecha de Creación", value=datetime.now().strftime("%Y-%m-%d"), disabled=True, key=f"f_crea_{run}")
        c3.text_input("Hora de Creación", value=datetime.now().strftime("%H:%M:%S"), disabled=True, key=f"h_crea_{run}")
        st.caption("El No. de Viaje definitivo se asigna al guardar, para evitar que dos digitadores reciban el mismo número.")

        cd_origen_final = cd_origen_fijo

        st.subheader("1. Selección de Transporte")
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

        st.subheader("2. Destinos y Carga por Tienda")
        st.caption("Primero cuenta lo físico (cajas, tarimas, roles); el marchamo de ida se pone al final, "
                    "cuando ya cerraste el conteo de esa tienda.")

        if st.button("➕ Añadir Destino Adicional"):
            st.session_state.num_destinos += 1
            st.rerun()

        tiendas_cliente = st.session_state.catalogos["clientes"][cliente_activo]
        destinos_viaje = []
        total_destinos = st.session_state.num_destinos
        tiendas_usadas_en_form = set()

        for i in range(total_destinos):
            with st.container(border=True):
                st.markdown(f"#### 📍 Destino #{i + 1}")

                tienda = st.selectbox("Tienda / Destino", [""] + list(tiendas_cliente.keys()), key=f"t_{run}_{i}")
                km_t = tiendas_cliente[tienda]["km"] if tienda else 0.0
                gal_t = tiendas_cliente[tienda]["galones_base"] if tienda else 0.0
                st.caption(f"Distancia: {km_t} KM | Diésel: {gal_t} Gal")

                es_complemento = st.toggle(
                    "¿Es complemento? (el resto de un pedido que no cupo en un viaje anterior)",
                    key=f"comp_{run}_{i}"
                )
                if es_complemento:
                    st.caption("Solo se piden Roles y Tarimas — las cajas y P&G van en el otro vehículo.")

                # --- Lista de pedidos (memoria temporal) ---
                # Cada pedido agregado aquí suma sus cajas al total. Cuando exista la
                # conexión con el WMS, validar_pedido_wms() traerá el conteo real en vez
                # del que digita el usuario a mano.
                key_lista_pedidos = f"pedidos_lista_{run}_{i}"
                if key_lista_pedidos not in st.session_state:
                    st.session_state[key_lista_pedidos] = []
                lista_pedidos = st.session_state[key_lista_pedidos]

                if not es_complemento:
                    st.markdown("**📦 Cajas** — agrega los pedidos de esta tienda (o usa el total manual)")
                    with st.form(key=f"form_pedido_{run}_{i}", clear_on_submit=True):
                        fp1, fp2, fp3 = st.columns([2, 1, 1])
                        with fp1:
                            pedido_codigo = st.text_input("No. de Pedido", key=f"cod_pedido_{run}_{i}")
                        with fp2:
                            pedido_cajas = st.number_input("Cajas de este pedido", min_value=0, step=1, key=f"cajas_pedido_{run}_{i}")
                        with fp3:
                            st.write("")
                            agregar_pedido = st.form_submit_button("➕ Agregar")
                    if agregar_pedido:
                        if pedido_codigo.strip():
                            cajas_wms = validar_pedido_wms(pedido_codigo.strip())
                            lista_pedidos.append({
                                "pedido": pedido_codigo.strip(),
                                "cajas": cajas_wms if cajas_wms is not None else pedido_cajas,
                                "origen": "WMS" if cajas_wms is not None else "Manual"
                            })
                            st.rerun()
                        else:
                            st.warning("Escribe un número de pedido antes de agregarlo.")

                    if lista_pedidos:
                        for idx, p in enumerate(lista_pedidos):
                            pc1, pc2, pc3, pc4 = st.columns([2, 1, 1, 1])
                            pc1.write(f"📄 {p['pedido']}")
                            pc2.write(f"{p['cajas']} cajas")
                            pc3.write(f"_{p['origen']}_")
                            if pc4.button("🗑️", key=f"del_pedido_{run}_{i}_{idx}"):
                                lista_pedidos.pop(idx)
                                st.rerun()
                        cajas_total = sum(p["cajas"] for p in lista_pedidos)
                        st.number_input("Total Cajas (suma de pedidos)", value=cajas_total, disabled=True, key=f"c_calc_{run}_{i}")
                    else:
                        cajas_total = st.number_input("Cajas (total manual, sin pedidos detallados)", min_value=0, step=1, key=f"c_{run}_{i}")
                else:
                    cajas_total = 0
                    st.caption("📦 Cajas: no aplica en un complemento.")

                st.markdown("**🧱 Material físico**")
                mc1, mc2, mc3 = st.columns(3)
                with mc1:
                    tarimas = st.number_input("Tarimas Madera", min_value=0, step=1, key=f"tar_{run}_{i}")
                with mc2:
                    roles = st.number_input("Roles Metálicos", min_value=0, step=1, key=f"r_{run}_{i}")
                with mc3:
                    if not es_complemento:
                        pg_cajas = st.number_input("Cajas P&G (Procter & Gamble)", min_value=0, step=1, key=f"pg_{run}_{i}")
                    else:
                        pg_cajas = 0
                        st.caption("P&G: no aplica en un complemento.")

                st.markdown("**📄 Documentos de la tienda**")
                dc1, dc2, dc3 = st.columns(3)
                with dc1:
                    remitos_txt = st.text_input("Remisión", key=f"remitos_{run}_{i}", max_chars=10, placeholder="10 caracteres")
                with dc2:
                    devolucion_txt = st.text_input("Devolución", key=f"dev_{run}_{i}", max_chars=10, placeholder="10 caracteres")
                with dc3:
                    creditos_txt = st.text_input("Créditos", key=f"cred_{run}_{i}", max_chars=10, placeholder="10 caracteres")

                observaciones_txt = st.text_area(
                    "📝 Observaciones", key=f"obs_{run}_{i}",
                    placeholder="Ej: lleva transferencia T-123, tienda cerrada, faltante detectado, etc."
                )

                st.markdown("**🔒 Marchamo de Ida**")
                m_ida_tienda = st.text_input("Marchamo IDA (Único, se cierra al terminar esta tienda)", key=f"mida_{run}_{i}")

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
                        "marchamo_regreso": "",  # se completa más abajo, en el cierre del viaje
                        "roles": roles,
                        "tarimas": tarimas,
                        "cajas": cajas_total,
                        "remitos": remitos_txt.strip(),
                        "incidencias": observaciones_txt.strip(),
                        "devolucion": devolucion_txt.strip(),
                        "creditos": creditos_txt.strip(),
                        "pg_cajas": pg_cajas,
                        "es_complemento": es_complemento
                    })
            st.markdown("")

        st.subheader("3. Cierre del Viaje")
        st.caption("El Marchamo de Regreso se coloca al final, cuando ya se cerraron todas las tiendas.")
        marchamo_regreso_viaje = st.text_input("🔄 Marchamo de REGRESO (obligatorio)", key=f"mreg_final_{run}")
        if destinos_viaje:
            destinos_viaje[-1]["marchamo_regreso"] = marchamo_regreso_viaje.strip()

        if st.button("💾 Guardar Viaje y Generar Hoja de Control"):
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
                    st.session_state.num_destinos = 1
                    st.session_state.form_run += 1  # limpia el formulario para el próximo viaje
                    st.session_state["ultimo_viaje_guardado"] = resultado
                    st.rerun()
                else:
                    st.error(f"❌ Error: {resultado}")

        if st.session_state.get("ultimo_viaje_guardado"):
            st.markdown("---")
            if st.button(f"🖨️ Ver Hoja de Control del viaje {st.session_state['ultimo_viaje_guardado']}"):
                v, d = buscar_viaje(st.session_state["ultimo_viaje_guardado"])
                if v:
                    components.html(generar_hoja_control_html(v, d), height=900, scrolling=True)

        st.markdown("### 🕒 Últimos viajes registrados")
        st.dataframe(obtener_viajes_recientes(), use_container_width=True)
    else:
        st.info("Tu perfil no tiene permisos para despachar viajes.")

# ==========================================
# MÓDULO 2: LIQUIDACIONES
# ==========================================
with tab2:
    if perfil_activo in ["Administrador", "Liquidador"]:
        st.header("Liquidación de Viajes")
        st.caption("Registra lo que el camión trajo de regreso de cada tienda. Las cajas no se devuelven.")

        col_crit, col_val, col_btn = st.columns([1, 2, 1])
        with col_crit:
            criterio = st.selectbox("Buscar por", ["No. de Viaje", "Marchamo de Ida", "Placa"], key="criterio_liq")
        with col_val:
            valor_busqueda = st.text_input("Valor a buscar", key="valor_liq")
        with col_btn:
            st.write("")
            st.write("")
            buscar = st.button("🔍 Buscar Viaje")

        if buscar:
            if valor_busqueda.strip():
                viaje, destinos = buscar_viaje(valor_busqueda)
                st.session_state["viaje_liq"] = viaje
                st.session_state["destinos_liq"] = destinos
            else:
                st.warning("Escribe un valor para buscar.")

        viaje = st.session_state.get("viaje_liq")
        destinos = st.session_state.get("destinos_liq", [])

        if "viaje_liq" in st.session_state and viaje is None:
            st.warning("No se encontró ningún viaje con ese dato.")

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
                            "Roles devueltos", min_value=0, step=1, key=f"roldev_{d['id']}"
                        )
                    with c2:
                        tarimas_dev = st.number_input(
                            "Tarimas devueltas", min_value=0, step=1, key=f"tardev_{d['id']}"
                        )
                    with c3:
                        pacas_dev = st.number_input(
                            "Pacas de cartón devueltas", min_value=0, step=1, key=f"pacdev_{d['id']}"
                        )
                    destinos_actualizados.append({
                        "id": d["id"],
                        "roles_devueltos": roles_dev,
                        "tarimas_devueltas": tarimas_dev,
                        "pacas_carton_devueltas": pacas_dev
                    })
                    st.markdown("---")

                if st.button("✅ Liquidar Viaje"):
                    ok, msg = liquidar_viaje(viaje["id"], destinos_actualizados, usuario_activo)
                    if ok:
                        st.success(f"Viaje {viaje['id_viaje']} liquidado correctamente. "
                                   "Ya puede procesarse el pago al transportista.")
                        del st.session_state["viaje_liq"]
                        del st.session_state["destinos_liq"]
                        st.rerun()
                    else:
                        st.error(f"❌ Error al liquidar: {msg}")

                with st.expander("🚫 Anular este viaje"):
                    st.caption("Usa esto solo si el viaje se digitó por error. Queda registrado quién y cuándo "
                               "lo anuló; por ahora los marchamos usados quedan libres para digitarse en otro viaje.")
                    motivo_anulacion = st.text_input("Motivo de la anulación", key=f"motivo_anular_{viaje['id']}")
                    if st.button("🚫 Confirmar Anulación", key=f"btn_anular_{viaje['id']}"):
                        if not motivo_anulacion.strip():
                            st.warning("Escribe el motivo antes de anular.")
                        else:
                            ok, msg = anular_viaje(viaje["id"], usuario_activo, motivo_anulacion.strip())
                            if ok:
                                st.success(f"Viaje {viaje['id_viaje']} anulado.")
                                del st.session_state["viaje_liq"]
                                del st.session_state["destinos_liq"]
                                st.rerun()
                            else:
                                st.error(f"❌ Error al anular: {msg}")
    else:
        st.info("Tu perfil no tiene permisos para liquidar viajes.")

# ==========================================
# MÓDULO 3: REPORTES (pendiente de construir)
# ==========================================
with tab3:
    st.info("Módulo de reportes en construcción: impacto de diésel por viaje/tienda usando el "
            "precio configurado en la barra lateral.")
