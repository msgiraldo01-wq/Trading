import os
import math
import random
import sqlite3
import requests
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO

app = Flask(__name__)
app.config['SECRET_KEY'] = 'trading_secret_key_1234'
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'trading.db')

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════
MODO_REAL   = False
API_KEY     = ""
API_SECRET  = ""

CAPITAL_INICIAL  = 1000.00
STOP_LOSS_PCT    = 0.03
STOP_LOSS_USD    = round(CAPITAL_INICIAL * STOP_LOSS_PCT, 2)  # $30.00

URL_BINANCE      = "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT"

DISTANCIA_GRID   = 1.00
TAMANO_OPERACION = 0.02
CANTIDAD_NIVELES = 8
# ══════════════════════════════════════════════════════════════════════════════

# FIX 1: El precio ya NO arranca hardcodeado. Se inicializa consultando Binance
# o, si no hay red, tomando el precio promedio de las posiciones abiertas en BD.
# Esto evita el error de capital falso al reiniciar.
PRECIO_SIMULADO_ORO  = 0.0    # Se ajusta en inicializar_precio_inicial()
bot_corriendo        = False
precio_base_dinamico = None
pnl_flotante_actual  = 0.0


# ── BASE DE DATOS ──────────────────────────────────────────────────────────────

def obtener_conexion_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def inicializar_base_datos():
    conn = obtener_conexion_db()
    cur  = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS operaciones (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id              TEXT    NOT NULL,
            instrumento            TEXT    NOT NULL,
            tipo                   TEXT    NOT NULL,
            precio_ejecucion       REAL    NOT NULL,
            cantidad               REAL    NOT NULL,
            total_usd              REAL    NOT NULL,
            estado                 TEXT    NOT NULL,
            id_operacion_vinculada INTEGER,
            fecha_ejecucion        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS configuracion_grid (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            instrumento      TEXT    NOT NULL UNIQUE,
            distancia_precio REAL    NOT NULL,
            tamano_operacion REAL    NOT NULL,
            cantidad_niveles INTEGER NOT NULL
        )
    """)

    # FIX 2: Nueva tabla para persistir el último precio conocido
    cur.execute("""
        CREATE TABLE IF NOT EXISTS estado_sistema (
            clave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        )
    """)

    cur.execute("""
        INSERT INTO configuracion_grid (instrumento, distancia_precio, tamano_operacion, cantidad_niveles)
        VALUES ('XAU_USD', ?, ?, ?)
        ON CONFLICT(instrumento) DO UPDATE SET
            distancia_precio = excluded.distancia_precio,
            tamano_operacion = excluded.tamano_operacion,
            cantidad_niveles = excluded.cantidad_niveles
    """, (DISTANCIA_GRID, TAMANO_OPERACION, CANTIDAD_NIVELES))

    conn.commit()
    conn.close()
    print(f"[DB]     SQLite lista → {DB_PATH}")
    print(f"[CONFIG] Capital: ${CAPITAL_INICIAL} | Stop-Loss: {STOP_LOSS_PCT*100:.0f}% = ${STOP_LOSS_USD}")
    print(f"[GRID]   Distancia: ${DISTANCIA_GRID} | Tamaño: {TAMANO_OPERACION}oz | Niveles: {CANTIDAD_NIVELES}")
    print(f"[MODO]   {'🔴 REAL' if MODO_REAL else '🟡 SIMULACIÓN'}")


def inicializar_precio_inicial():
    """
    FIX PRINCIPAL: Al arrancar, determina el precio correcto en este orden:
    1. Consulta Binance en tiempo real (lo mejor)
    2. Usa el último precio guardado en BD (si Binance falla)
    3. Calcula el promedio de posiciones abiertas (fallback)
    4. Usa 4100.00 como último recurso
    Esto elimina el error de $14-16 de capital que se veía al reiniciar.
    """
    global PRECIO_SIMULADO_ORO
    # Intento 1: Binance
    try:
        r = requests.get(URL_BINANCE, timeout=3)
        if r.status_code == 200:
            PRECIO_SIMULADO_ORO = round(float(r.json()['price']), 2)
            guardar_precio_en_db(PRECIO_SIMULADO_ORO)
            print(f"[PRECIO] Inicializado desde Binance: ${PRECIO_SIMULADO_ORO}")
            return
    except Exception:
        pass

    # Intento 2: Último precio guardado en BD
    try:
        conn = obtener_conexion_db()
        cur  = conn.cursor()
        cur.execute("SELECT valor FROM estado_sistema WHERE clave='ultimo_precio'")
        row = cur.fetchone()
        conn.close()
        if row:
            PRECIO_SIMULADO_ORO = float(row['valor'])
            print(f"[PRECIO] Inicializado desde BD (último guardado): ${PRECIO_SIMULADO_ORO}")
            return
    except Exception:
        pass

    # Intento 3: Promedio de posiciones abiertas
    try:
        conn = obtener_conexion_db()
        cur  = conn.cursor()
        cur.execute("SELECT AVG(precio_ejecucion) FROM operaciones WHERE estado='ABIERTA' AND tipo='COMPRA'")
        avg = cur.fetchone()[0]
        conn.close()
        if avg:
            PRECIO_SIMULADO_ORO = round(float(avg), 2)
            print(f"[PRECIO] Inicializado desde promedio posiciones abiertas: ${PRECIO_SIMULADO_ORO}")
            return
    except Exception:
        pass

    # Fallback
    PRECIO_SIMULADO_ORO = 4100.00
    print(f"[PRECIO] Fallback: ${PRECIO_SIMULADO_ORO}")


def guardar_precio_en_db(precio):
    try:
        conn = obtener_conexion_db()
        conn.execute("""
            INSERT INTO estado_sistema (clave, valor) VALUES ('ultimo_precio', ?)
            ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor
        """, (str(precio),))
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── PRECIO EN TIEMPO REAL ──────────────────────────────────────────────────────

def obtener_precio_real_mercado():
    global PRECIO_SIMULADO_ORO
    try:
        r = requests.get(URL_BINANCE, timeout=2)
        if r.status_code == 200:
            precio = round(float(r.json()['price']), 2)
            guardar_precio_en_db(precio)  # FIX: persistir siempre el último precio real
            return precio
        print(f"[BINANCE] Status {r.status_code}. Modo simulado activo.")
    except Exception as e:
        print(f"[BINANCE] Error: {e}. Modo simulado activo.")

    variacion = random.uniform(-0.50, 0.50)
    PRECIO_SIMULADO_ORO = round(PRECIO_SIMULADO_ORO + variacion, 2)
    return PRECIO_SIMULADO_ORO


# ── P&L FLOTANTE ───────────────────────────────────────────────────────────────

def calcular_pnl_flotante(precio_actual):
    try:
        conn = obtener_conexion_db()
        cur  = conn.cursor()
        cur.execute("""
            SELECT precio_ejecucion, cantidad FROM operaciones
            WHERE instrumento='XAU_USD' AND estado='ABIERTA' AND tipo='COMPRA'
        """)
        rows = cur.fetchall()
        cur.close(); conn.close()
        return sum((precio_actual - float(r['precio_ejecucion'])) * float(r['cantidad']) for r in rows)
    except:
        return 0.0


# ── MOTOR DEL BOT ──────────────────────────────────────────────────────────────

def simulador_mercado_y_bot():
    global PRECIO_SIMULADO_ORO, bot_corriendo, precio_base_dinamico, pnl_flotante_actual
    print("-> Hilo del bot activo y escuchando...")

    while True:
        if bot_corriendo:
            precio = obtener_precio_real_mercado()
            if precio:
                PRECIO_SIMULADO_ORO = precio

                if precio_base_dinamico is None:
                    precio_base_dinamico = precio
                    print(f"[GRID] Pivote fijado en: ${precio_base_dinamico}")

                pnl_flotante_actual = calcular_pnl_flotante(precio)
                sl_pct_usado = round(
                    min(abs(min(pnl_flotante_actual, 0)) / STOP_LOSS_USD * 100, 100), 1
                ) if pnl_flotante_actual < 0 else 0.0

                socketio.emit('actualizacion_precio', {
                    'precio':              precio,
                    'pnl_flotante':        round(pnl_flotante_actual, 4),
                    'stop_loss_pct_usado': sl_pct_usado
                })

                evaluar_logica_grid(precio)

        socketio.sleep(2.0)


def evaluar_logica_grid(precio_actual):
    global bot_corriendo
    try:
        conn = obtener_conexion_db()
        cur  = conn.cursor()

        cur.execute("SELECT * FROM configuracion_grid WHERE instrumento='XAU_USD' LIMIT 1")
        config = cur.fetchone()
        if not config:
            conn.close(); return

        distancia   = float(config['distancia_precio'])
        tamano      = float(config['tamano_operacion'])
        max_niveles = int(config['cantidad_niveles'])

        cur.execute("""
            SELECT * FROM operaciones
            WHERE instrumento='XAU_USD' AND estado='ABIERTA' AND tipo='COMPRA'
        """)
        compras_abiertas  = cur.fetchall()
        precios_comprados = [float(op['precio_ejecucion']) for op in compras_abiertas]

        # ── STOP-LOSS ────────────────────────────────────────────────────────
        pnl = sum(
            (precio_actual - float(op['precio_ejecucion'])) * float(op['cantidad'])
            for op in compras_abiertas
        )
        if pnl <= -STOP_LOSS_USD and compras_abiertas:
            print(f"[STOP-LOSS] 🚨 Activado. P&L: ${pnl:.2f} | Límite: -${STOP_LOSS_USD}")
            perdida_total = 0.0
            for pos in compras_abiertas:
                total_venta = precio_actual * float(pos['cantidad'])
                ticket_sl   = f"TICK-SL-{random.randint(100000, 999999)}"
                cur.execute("""
                    INSERT INTO operaciones
                        (ticket_id, instrumento, tipo, precio_ejecucion, cantidad,
                         total_usd, estado, id_operacion_vinculada)
                    VALUES (?, 'XAU_USD', 'VENTA_SL', ?, ?, ?, 'CERRADA', ?)
                """, (ticket_sl, precio_actual, pos['cantidad'], total_venta, pos['id']))
                cur.execute("UPDATE operaciones SET estado='CERRADA' WHERE id=?", (pos['id'],))
                perdida_total += total_venta - float(pos['total_usd'])
            conn.commit()
            cur.close(); conn.close()
            bot_corriendo = False
            socketio.emit('stop_loss_activado', {
                'msg':     f"🚨 STOP-LOSS: Pérdida de ${abs(perdida_total):.2f} realizada. Bot detenido.",
                'perdida': round(perdida_total, 2)
            })
            return

        # ── COMPRA ───────────────────────────────────────────────────────────
        nivel_teorico   = math.floor(precio_actual / distancia) * distancia
        existe_en_nivel = any(abs(p - nivel_teorico) < (distancia * 0.5) for p in precios_comprados)

        if not existe_en_nivel and len(compras_abiertas) < max_niveles:
            ticket    = f"TICK-BUY-{random.randint(100000, 999999)}"
            total_usd = nivel_teorico * tamano
            cur.execute("""
                INSERT INTO operaciones
                    (ticket_id, instrumento, tipo, precio_ejecucion, cantidad, total_usd, estado)
                VALUES (?, 'XAU_USD', 'COMPRA', ?, ?, ?, 'ABIERTA')
            """, (ticket, nivel_teorico, tamano, total_usd))
            conn.commit()
            socketio.emit('nueva_operacion', {
                'msg':    f"🛒 COMPRA: {tamano}oz @ ${nivel_teorico:.2f}",
                'tipo':   'COMPRA', 'precio': nivel_teorico
            })

        # ── TAKE PROFIT ──────────────────────────────────────────────────────
        for compra in compras_abiertas:
            precio_compra = float(compra['precio_ejecucion'])
            if precio_actual >= precio_compra + distancia:
                total_venta  = precio_actual * float(compra['cantidad'])
                ganancia     = total_venta - float(compra['total_usd'])
                ticket_venta = f"TICK-SELL-{random.randint(100000, 999999)}"
                cur.execute("""
                    INSERT INTO operaciones
                        (ticket_id, instrumento, tipo, precio_ejecucion, cantidad,
                         total_usd, estado, id_operacion_vinculada)
                    VALUES (?, 'XAU_USD', 'VENTA', ?, ?, ?, 'CERRADA', ?)
                """, (ticket_venta, precio_actual, compra['cantidad'], total_venta, compra['id']))
                cur.execute("UPDATE operaciones SET estado='CERRADA' WHERE id=?", (compra['id'],))
                conn.commit()
                socketio.emit('nueva_operacion', {
                    'msg':    f"💰 VENTA +${ganancia:.4f}: ${precio_compra:.2f} → ${precio_actual:.2f}",
                    'tipo':   'VENTA', 'precio': precio_actual
                })

        cur.close(); conn.close()
    except Exception as e:
        print(f"[ERROR grid] {e}")


# ── RUTAS API ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/estado', methods=['GET'])
def obtener_estado():
    try:
        conn = obtener_conexion_db()
        cur  = conn.cursor()

        cur.execute("SELECT * FROM operaciones ORDER BY fecha_ejecucion DESC LIMIT 20")
        operaciones = [dict(row) for row in cur.fetchall()]

        cur.execute("SELECT COUNT(*) FROM operaciones WHERE estado='ABIERTA' AND tipo='COMPRA'")
        n_abiertas = cur.fetchone()[0]

        cur.execute("SELECT * FROM configuracion_grid WHERE instrumento='XAU_USD' LIMIT 1")
        cfg = cur.fetchone()
        dist_grid = float(cfg['distancia_precio']) if cfg else DISTANCIA_GRID

        # Posiciones abiertas con P&L individual
        cur.execute("""
            SELECT id, ticket_id, precio_ejecucion, cantidad, total_usd, fecha_ejecucion,
                   ROUND((? - precio_ejecucion) * cantidad, 6) as pnl_pos,
                   ROUND(precio_ejecucion + ?, 2)              as precio_tp
            FROM operaciones
            WHERE instrumento='XAU_USD' AND estado='ABIERTA' AND tipo='COMPRA'
            ORDER BY precio_ejecucion DESC
        """, (PRECIO_SIMULADO_ORO, dist_grid))
        posiciones_detalle = []
        for r in cur.fetchall():
            p = dict(r)
            for k in ('precio_ejecucion','cantidad','total_usd','pnl_pos','precio_tp'):
                p[k] = float(p[k])
            posiciones_detalle.append(p)

        cur.close(); conn.close()

        # P&L realizado correcto via JOIN
        conn2 = obtener_conexion_db()
        cur2  = conn2.cursor()
        cur2.execute("""
            SELECT COALESCE(SUM(v.total_usd - c.total_usd), 0)
            FROM operaciones v
            JOIN operaciones c ON v.id_operacion_vinculada = c.id
            WHERE v.instrumento='XAU_USD' AND v.tipo IN ('VENTA','VENTA_SL')
        """)
        ganancia_realizada = float(cur2.fetchone()[0])
        cur2.close(); conn2.close()

        pnl_flotante   = calcular_pnl_flotante(PRECIO_SIMULADO_ORO)
        capital_actual = CAPITAL_INICIAL + ganancia_realizada + pnl_flotante
        sl_pct_usado   = round(
            min(abs(min(pnl_flotante, 0)) / STOP_LOSS_USD * 100, 100), 1
        ) if pnl_flotante < 0 else 0.0

        for op in operaciones:
            for k in ('precio_ejecucion','cantidad','total_usd'):
                op[k] = float(op[k])

        return jsonify({
            'bot_corriendo':       bot_corriendo,
            'modo':                'REAL' if MODO_REAL else 'SIMULACIÓN',
            'precio_actual':       PRECIO_SIMULADO_ORO,
            'capital_inicial':     CAPITAL_INICIAL,
            'capital_actual':      round(capital_actual, 2),
            'ganancia_realizada':  round(ganancia_realizada, 4),
            'pnl_flotante':        round(pnl_flotante, 4),
            'stop_loss_usd':       STOP_LOSS_USD,
            'stop_loss_pct':       STOP_LOSS_PCT * 100,
            'stop_loss_pct_usado': sl_pct_usado,
            'posiciones_abiertas': n_abiertas,
            'posiciones_detalle':  posiciones_detalle,
            'config_grid': {
                'distancia': float(cfg['distancia_precio']) if cfg else DISTANCIA_GRID,
                'tamano':    float(cfg['tamano_operacion']) if cfg else TAMANO_OPERACION,
                'niveles':   int(cfg['cantidad_niveles'])   if cfg else CANTIDAD_NIVELES
            },
            'historial': operaciones
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/control', methods=['POST'])
def controlar_bot():
    global bot_corriendo, precio_base_dinamico
    data = request.get_json(force=True)
    bot_corriendo = data.get('encender', False)
    if not bot_corriendo:
        precio_base_dinamico = None
        print("[CONTROL] Bot detenido.")
    else:
        print("[CONTROL] Bot encendido.")
    return jsonify({'status': 'ok', 'bot_corriendo': bot_corriendo})


@app.route('/api/historico', methods=['GET'])
def obtener_historico():
    desde = request.args.get('desde', '')
    hasta = request.args.get('hasta', '')
    try:
        conn = obtener_conexion_db()
        cur  = conn.cursor()

        cond   = ["instrumento='XAU_USD'"]
        params = []
        if desde: cond.append("DATE(fecha_ejecucion) >= ?"); params.append(desde)
        if hasta: cond.append("DATE(fecha_ejecucion) <= ?"); params.append(hasta)
        where = " AND ".join(cond)

        cur.execute(f"SELECT * FROM operaciones WHERE {where} ORDER BY fecha_ejecucion DESC LIMIT 50", params)
        ops = [dict(r) for r in cur.fetchall()]

        # P&L por día via JOIN correcto
        vparams = []
        vcond   = ["v.instrumento='XAU_USD'", "v.tipo IN ('VENTA','VENTA_SL')"]
        if desde: vcond.append("DATE(v.fecha_ejecucion) >= ?"); vparams.append(desde)
        if hasta: vcond.append("DATE(v.fecha_ejecucion) <= ?"); vparams.append(hasta)
        vwhere = " AND ".join(vcond)

        cur.execute(f"""
            SELECT DATE(v.fecha_ejecucion) as dia,
                   SUM(v.total_usd - c.total_usd)                          as pnl_dia,
                   COUNT(*)                                                  as n_ventas,
                   SUM(CASE WHEN v.tipo='VENTA_SL' THEN 1 ELSE 0 END)      as n_sl
            FROM operaciones v
            JOIN operaciones c ON v.id_operacion_vinculada = c.id
            WHERE {vwhere}
            GROUP BY DATE(v.fecha_ejecucion) ORDER BY dia ASC
        """, vparams)

        cur2 = conn.cursor()
        cur2.execute(f"""
            SELECT DATE(fecha_ejecucion) as dia, COUNT(*) as n_compras
            FROM operaciones WHERE {where} AND tipo='COMPRA'
            GROUP BY DATE(fecha_ejecucion)
        """, params)
        compras_x_dia = {r['dia']: r['n_compras'] for r in cur2.fetchall()}

        por_dia  = []
        pnl_acum = 0.0
        for row in cur.fetchall():
            d = dict(row)
            d['pnl_dia']   = round(float(d['pnl_dia'] or 0), 6)
            d['n_compras'] = compras_x_dia.get(d['dia'], 0)
            d['n_sl']      = int(d['n_sl'] or 0)
            pnl_acum      += d['pnl_dia']
            d['pnl_acum']  = round(pnl_acum, 6)
            d['capital']   = round(CAPITAL_INICIAL + pnl_acum, 2)
            por_dia.append(d)

        n_ventas = sum(d['n_ventas'] for d in por_dia)
        n_sl     = sum(d['n_sl']     for d in por_dia)

        cur.close(); conn.close()

        for op in ops:
            for k in ('precio_ejecucion','cantidad','total_usd'):
                op[k] = float(op[k])

        return jsonify({
            'por_dia':    por_dia,
            'operaciones': ops,
            'resumen': {
                'pnl_total':     round(pnl_acum, 6),
                'capital_final': round(CAPITAL_INICIAL + pnl_acum, 2),
                'n_compras':     sum(d['n_compras'] for d in por_dia),
                'n_ventas_tp':   n_ventas - n_sl,
                'n_stop_loss':   n_sl,
                'win_rate':      round((n_ventas - n_sl) / max(n_ventas, 1) * 100, 1)
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/resetear', methods=['POST'])
def resetear_simulacion():
    global bot_corriendo, precio_base_dinamico, pnl_flotante_actual
    bot_corriendo = False; precio_base_dinamico = None; pnl_flotante_actual = 0.0
    try:
        conn = obtener_conexion_db()
        conn.execute("DELETE FROM operaciones")
        conn.execute("DELETE FROM estado_sistema WHERE clave='ultimo_precio'")
        conn.commit(); conn.close()
        print(f"[RESET] Simulación reiniciada. Capital: ${CAPITAL_INICIAL}")
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── ARRANQUE ───────────────────────────────────────────────────────────────────

# ── ARRANQUE ───────────────────────────────────────────────────────────────────

inicializar_base_datos()
inicializar_precio_inicial()
socketio.start_background_task(simulador_mercado_y_bot)

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5001, use_reloader=False)