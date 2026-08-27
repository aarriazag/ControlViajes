import streamlit as st
import pandas as pd
from datetime import datetime

# Configuración de la página web
st.set_page_config(page_title="TMS - Control Logístico Avanzado", layout="wide", page_icon="🚛")

# ==========================================
# 1. BASE DE DATOS INICIAL (CATÁLOGOS MAESTROS)
# ==========================================
if 'db' not in st.session_state:
    st.session_state.db = {
        "usuarios": {
            "Admin_Logistica": "Administrador",
            "Op_Salidas": "Operador",
            "Liq_Transporte": "Liquidador"
        },
        "transportistas": ["Transportes Express", "Logística del Norte", "Flota Interna"],
        "clientes": {
            "Supermercados Alfa": {
                "Alfa Central": {"km": 15.5, "galones_base": 3.5},
                "Alfa Norte": {"km": 32.0, "galones_base": 7.0},
                "Alfa Sur": {"km": 8.4, "galones_base": 2.0}
            },
            "Hiper Tiendas Beta": {
                "Beta Este": {"km": 22.1, "galones_base": 5.0},
                "Beta Oeste": {"km": 45.3, "galones_base": 10.0}
            }
        },
        "camiones": {
            "C-123ABC": {"tipo": "5 Ton", "transportista": "Transportes Express", "piloto": "Juan Pérez", "auxiliar": "Carlos López"},
            "C-456DEF": {"tipo": "10 Ton", "transportista": "Logística del Norte", "piloto": "María Rodríguez", "auxiliar": "Pedro Gómez"},
            "C-789GHI": {"tipo": "20 Ton", "transportista": "Flota Interna", "piloto": "Luis Martínez", "auxiliar": "José Hernández"}
        },
        "pilotos": ["Juan Pérez", "María Rodríguez", "Luis Martínez", "Andrés Custodio"],
        "auxiliares": ["Carlos López", "Pedro Gómez", "José Hernández", "Ramiro Ruiz"]
    }

if 'viajes' not in st.session_state:
    st.session_state.viajes = []

if 'marchamos' not in st.session_state:
    st.session_state.marchamos = set()

# ==========================================
# 2. CONTROL DE ACCESO (PERFILES)
# ==========================================
st.sidebar.title("🔐 Control de Acceso")
usuario_activo = st.sidebar.selectbox("Simular Usuario Activo", list(st.session_state.db["usuarios"].keys()))
perfil_activo = st.session_state.db["usuarios"][usuario_activo]
st.sidebar.info(f"**Perfil:** {perfil_activo}")

# Variable global para el precio del diésel simulado de la semana
st.sidebar.markdown("---")
st.sidebar.subheader("⛽ Parámetros Económicos")
precio_diesel_semana = st.sidebar.number_input("Precio Diésel por Galón ($)", min_value=1.0, value=4.50, step=0.10)

# Título Principal
st.title("🚛 Sistema Integral de Gestión Logística (TMS)")
st.markdown("---")

# Pestañas de Navegación según el flujo solicitado
tabs = st.tabs(["📋 Despacho (Salidas)", "💰 Recepción (Liquidaciones)", "📊 Reportes e Impacto Diésel"])

# ==========================================
# MÓDULO 1: DESPACHO / CREACIÓN DE VIAJES
# ==========================================
with tabs[0]:
    if perfil_activo in ["Administrador", "Operador"]:
        st.header("Generación de Hoja de Control de Viaje")
        
        # Correlativo automático y metadatos
        id_viaje = len(st.session_state.viajes) + 1
        fecha_hoy = datetime.now().strftime("%Y-%m-%d")
        hora_hoy = datetime.now().strftime("%H:%M:%S")
        
        c1, c2, c3 = st.columns(3)
        c1.metric("No. Viaje Correlativo", f"#{id_viaje}")
        c2.text_input("Fecha de Creación", value=fecha_hoy, disabled=True, key="f_crea")
        c3.text_input("Hora de Creación", value=hora_hoy, disabled=True, key="h_crea")
        
        st.subheader("1. Selección de Transporte")
        col_p, col_t, col_cap, col_pil, col_aux = st.columns(5)
        
        with col_p:
            placa = st.selectbox("Placa del Camión", [""] + list(st.session_state.db["camiones"].keys()))
            
        # Lógica automatizada pero editable
        t_pred, cap_pred, pil_pred, aux_pred = "", "", "", ""
        if placa:
            datos_c = st.session_state.db["camiones"][placa]
            t_pred = datos_c["transportista"]
            cap_pred = datos_c["tipo"]
            pil_pred = datos_c["piloto"]
            aux_pred = datos_c["auxiliar"]
            
        with col_t: st.text_input("Transportista", value=t_pred, disabled=True)
        with col_cap: st.text_input("Capacidad Camión", value=cap_pred, disabled=True)
        with col_pil: piloto_final = st.selectbox("Piloto", st.session_state.db["pilotos"], index=st.session_state.db["pilotos"].index(pil_pred) if pil_pred in st.session_state.db["pilotos"] else 0)
        with col_aux: auxiliar_final = st.selectbox("Auxiliar de Carga", st.session_state.db["auxiliares"], index=st.session_state.db["auxiliares"].index(aux_pred) if aux_pred in st.session_state.db["auxiliares"] else 0)
        
        st.subheader("2. Seguridad (Marchamos de Control)")
        cm1, cm2 = st.columns(2)
        m_ida = cm1.text_input("No. Marchamo de Ida (Único)")
        m_regreso = cm2.text_input("No. Marchamo de Regreso (Última Tienda)")
        
        st.subheader("3. Asignación de Cliente y Destinos Aislados")
        cliente_sel = st.selectbox("Seleccione el Cliente del Viaje", [""] + list(st.session_state.db["clientes"].keys()))
        
        destinos_viaje = []
        if cliente_sel:
            # Si hay cliente seleccionado, habilita agregar destinos dinámicos exclusivos de ese cliente
            if f'num_dest_{id_viaje}' not in st.session_state:
                st.session_state[f'num_dest_{id_viaje}'] = 1
                
            if st.button("➕ Añadir Destino Adicional"):
                st.session_state[f'num_dest_{id_viaje}'] += 1
                st.rerun()
                
            tiendas_cliente = st.session_state.db["clientes"][cliente_sel]
            
            for i in range(st.session_state[f'num_dest_{id_viaje}']):
                st.markdown(f"**Destino #{i+1}**")
                d1, d2, d3, d4, d5 = st.columns([2, 2, 1, 1, 1])
                
                with d1:
                    tienda = d1.selectbox("Destino / Tienda", [""] + list(tiendas_cliente.keys()), key=f"t_{id_viaje}_{i}")
                    km_t = tiendas_cliente[tienda]["km"] if tienda else 0.0
                    gal_t = tiendas_cliente[tienda]["galones_base"] if tienda else 0.0
                    st.caption(f"Distancia: {km_t} KM | Consumo Base: {gal_t} Gal")
                with d2: peds = d2.text_input("No. Pedidos (Separados por coma)", key=f"p_{id_viaje}_{i}", placeholder="P001, P002")
                with d3: roles = d3.number_input("Roles", min_value=0, step=1, key=f"r_{id_viaje}_{i}")
                with d4: tarimas = d4.number_input("Tarimas", min_value=0, step=1, key=f"tar_{id_viaje}_{i}")
                with d5: cajas = d5.number_input("Cajas (WMS/Manual)", min_value=0, step=1, key=f"c_{id_viaje}_{i}")
                
                if tienda:
                    destinos_viaje.append({
                        "tienda": tienda, "km": km_t, "galones_base": gal_t,
                        "pedidos": peds, "roles": roles, "tarimas": tarimas, "cajas": cajas
                    })
                    
        st.markdown("---")
        if st.button("💾 Validar, Guardar Emisión e Imprimir"):
            if not placa or not m_ida or not m_regreso or not cliente_sel or len(destinos_viaje) == 0:
                st.error("❌ Faltan campos obligatorios o destinos por llenar.")
            elif m_ida in st.session_state.marchamos:
                st.error(f"❌ El Marchamo de Ida '{m_ida}' ya fue utilizado en otro viaje del sistema.")
            else:
                # Guardar registro maestro del viaje
                nuevo_viaje = {
                    "id_viaje": id_viaje, "usuario_creador": usuario_activo, "fecha_creacion": fecha_hoy, "hora_creacion": hora_hoy,
                    "placa": placa, "transportista": t_pred, "tipo_camion": cap_pred, "piloto": piloto_final, "auxiliar": auxiliar_final,
                    "marchamo_ida": m_ida, "marchamo_regreso": m_regreso, "cliente": cliente_sel, "destinos": destinos_viaje,
                    "estado": "En Ruta", "usuario_liquida": None, "fecha_liquida": None, "retornos": {}
                }
                st.session_state.viajes.append(nuevo_viaje)
                st.session_state.marchamos.add(m_ida)
                st.success(f"✅ Viaje #{id_viaje} guardado con éxito. Estado: EN RUTA.")
                
                # Renderizar Hoja Física Consolidada para impresión directa
                st.markdown("### 🖨️ Documento de Control de Salida Listo para Impresión")
                html_print = f"""
                <div style="border:2px dashed #000; padding:15px; font-family:Courier, monospace; background-color:#fff; color:#000;">
                    <h3 style="text-align:center; margin:0;">HOJA DE CONTROL Y DESPACHO - VIAJE #{id_viaje}</h3>
                    <p style="text-align:center; margin:5px;"><b>Cliente:</b> {cliente_sel} | <b>Estado:</b> EN RUTA</p>
                    <hr>
                    <b>Despachador:</b> {usuario_activo} | <b>Fecha:</b> {fecha_hoy} | <b>Hora:</b> {hora_hoy}<br>
                    <b>Placa:</b> {placa} ({cap_pred}) | <b>Transportista:</b> {t_pred}<br>
                    <b>Piloto:</b> {piloto_final} | <b>Auxiliar:</b> {auxiliar_final}<br>
                    <b>Marchamos -> Ida:</b> {m_ida} | <b>Regreso Requerido:</b> {m_regreso}
                    <hr>
                    <h4>MANIFIESTO DE CARGA POR TIENDA</h4>
                    <table style="width:100%; border-collapse:collapse; border:1px solid #000; font-size:12px;">
                        <thead>
                            <tr style="background-color:#eee;">
                                <th style="border:1px solid #000; padding:4px;">Destino</th>
                                <th style="border:1px solid #000; padding:4px;">Pedidos</th>
                                <th style="border:1px solid #000; padding:4px;">Rol</th>
