# -*- coding: utf-8 -*-
# ★ للتعديل: افتح EDIT_MAP.txt - ابحث في الملف ده عن [EDIT-MAP] عشان توصل للأماكن المهمة ★
"""
سيرفر نقطة البيع - إستاكوزا  (Istakoza POS server)
- بايثون فقط (من غير مكتبات خارجية): http.server + sqlite3
- بيقدّم الصفحة pos_istakoza.html وبيحفظ كل البيانات في قاعدة SQLite (pos_data.db)
- مكان البيانات: C:\\ProgramData\\Istakoza  (لما يتشغل كـ exe)  أو فولدر البرنامج (لما يتشغل من السورس)
  ممكن تغييره بمتغير البيئة ISTAKOZA_DATA، والبورت بـ ISTAKOZA_PORT (الافتراضي 8080)
"""
import os, sys, re, json, base64, hashlib, secrets, threading, queue, socket, ipaddress, sqlite3, webbrowser, subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ★★★ [EDIT-MAP] رقم إصدار السيرفر (لو غيّرته غيّر معاه pos-ui-version في أول الصفحة) ★★★
VERSION = 11
FROZEN = getattr(sys, 'frozen', False)
APP_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))


def data_dir():
    d = os.environ.get('ISTAKOZA_DATA')
    if not d and os.name == 'nt':
        d = os.path.join(os.environ.get('PROGRAMDATA', APP_DIR), 'Istakoza')
    d = d or APP_DIR
    os.makedirs(d, exist_ok=True)
    return d


def resource_path(rel):
    # لو شغال كـ exe: الصفحة متحشورة جوه الـ exe (PyInstaller --add-data) وبتتفك في sys._MEIPASS
    base = getattr(sys, '_MEIPASS', None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


def ui_version(path):
    try:
        m = re.search(r'name="pos-ui-version" content="(\d+)"', open(path, encoding='utf-8').read(65536))
        return int(m.group(1)) if m else 0
    except OSError:
        return 0


def find_html():
    # 1) نسخة معدّلة اختيارية في فولدر الداتا (تتجاهل لو أقدم من السيرفر عشان ماتبوّظش التوافق)
    # 2) الصفحة المتحشورة جوه الـ exe  3) ملف جنب البرنامج (للتشغيل من السورس)
    custom = os.path.join(data_dir(), 'pos_istakoza.html')
    if os.path.isfile(custom):
        if ui_version(custom) >= VERSION:
            return custom
        print('تجاهل pos_istakoza.html القديمة في فولدر الداتا (إصدارها أقدم من السيرفر):', custom)
    for p in (resource_path('pos_istakoza.html'), os.path.join(APP_DIR, 'pos_istakoza.html')):
        if os.path.isfile(p):
            return p
    return resource_path('pos_istakoza.html')


DB = os.path.join(data_dir(), 'pos_data.db')
HTML = find_html()
PORT = int(os.environ.get('ISTAKOZA_PORT', '8080'))
LOCK = threading.Lock()
TOK = {}
SUBS, SUBS_LOCK = set(), threading.Lock()


def now_s():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ------------------------------------------------------------------ live events (SSE)
def broadcast(ev):
    with SUBS_LOCK:
        for q in list(SUBS):
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


# ------------------------------------------------------------------ database
# ★★★ [EDIT-MAP] جداول قاعدة البيانات (SQLite) ★★★
T = {
    'Products': 'PID INTEGER PRIMARY KEY,PName TEXT,Cat TEXT,Unit TEXT,Price REAL,Icon TEXT,Code TEXT,Hidden INTEGER DEFAULT 0,Cost REAL DEFAULT 0',
    'Stock': 'PID INTEGER PRIMARY KEY,Qty REAL',
    'Users': 'UName TEXT PRIMARY KEY,PHash TEXT,Role TEXT,FullName TEXT',
    'Settings': 'K TEXT PRIMARY KEY,V TEXT',
    'Sales': 'SID TEXT PRIMARY KEY,SNo INTEGER,SDay TEXT,STime TEXT,SHour INTEGER,SType TEXT,Cashier TEXT,CName TEXT,CPhone TEXT,CAddr TEXT,SSub REAL,SDisc REAL,STax REAL,SFee REAL,STotal REAL,Closed INTEGER DEFAULT 0',
    'SaleItems': 'SID TEXT,PID INTEGER,IName TEXT,Cat TEXT,Unit TEXT,Qty REAL,Price REAL',
    # v2: سجل الإلغاء والتعديل
    # v3: قفل الأيام بعد التقفيل النهائي
    'day_locks': 'day TEXT PRIMARY KEY,locked_at TEXT,locked_by TEXT',
    # v4: مرتجع + طلبيات الشراء
    'Returns': 'RID TEXT PRIMARY KEY,RNo INTEGER,SID TEXT,SNo INTEGER,RDay TEXT,RTime TEXT,Reason TEXT,Cashier TEXT,Manager TEXT,Total REAL,Status TEXT,PaidBy TEXT,PaidAt TEXT,Log TEXT',
    'ReturnItems': 'RID TEXT,PID INTEGER,IName TEXT,Cat TEXT,Unit TEXT,Qty REAL,Price REAL,Refund REAL',
    'Suppliers': 'SupID INTEGER PRIMARY KEY AUTOINCREMENT,SName TEXT,Phone TEXT,Note TEXT',
    'StoreItems': 'Code TEXT PRIMARY KEY,IName TEXT,Unit TEXT,Cat TEXT,Price REAL,SupID INTEGER',
    'PoTemplates': 'TID INTEGER PRIMARY KEY AUTOINCREMENT,TName TEXT,Items TEXT',
    'POrders': 'POID TEXT PRIMARY KEY,PONo TEXT,PODay TEXT,POTime TEXT,Supplier TEXT,Total REAL,Status TEXT,CreatedBy TEXT,SentAt TEXT,Note TEXT,Err TEXT',
    'POItems': 'POID TEXT,Code TEXT,IName TEXT,Unit TEXT,Qty REAL,Price REAL',
    # v6: مخزن الأكواد (بيزيد لما الطلبية تتسجل "تم الاستلام")
    'StoreStock': 'Code TEXT PRIMARY KEY,Qty REAL',
    # v9: جرد الشهر (رصيد أول المدة + وارد الطلبيات - المبيعات = المتوقع، والمقارنة بالعد الفعلي)
    'Stocktake': 'Month TEXT,PID INTEGER,Opening REAL,Actual REAL,PRIMARY KEY(Month,PID)',
    # v10: الريسبي (مكونات الصنف من الخامات) + تسويات الجرد + عدّ الجرد بالكود
    'Recipe': 'PID INTEGER,Code TEXT,Qty REAL,PRIMARY KEY(PID,Code)',
    'StoreAdj': 'Month TEXT,Code TEXT,Kind TEXT,Day TEXT,Qty REAL,PRIMARY KEY(Month,Code,Kind)',
    'StockCount': 'Month TEXT,Code TEXT,Opening REAL,Actual REAL,PRIMARY KEY(Month,Code)',
    'StocktakeMeta': 'Month TEXT PRIMARY KEY,Status TEXT,ClosedBy TEXT,ClosedAt TEXT',
    'SaleLog': 'LID INTEGER PRIMARY KEY AUTOINCREMENT,SID TEXT,Act TEXT,ByUser TEXT,Reason TEXT,At TEXT,Detail TEXT',
    'Drivers': 'DID INTEGER PRIMARY KEY AUTOINCREMENT,UName TEXT UNIQUE,FullName TEXT,Active INTEGER DEFAULT 1,CreatedAt TEXT',
    'DriverShifts': "ShiftID INTEGER PRIMARY KEY AUTOINCREMENT,DID INTEGER,OpenedAt TEXT,ClosedAt TEXT,OpeningCash REAL DEFAULT 0,ClosingCash REAL,ExpectedCash REAL DEFAULT 0,Status TEXT DEFAULT 'open',OpenedBy TEXT,ClosedBy TEXT,Note TEXT",
    'DriverOrders': "DOID INTEGER PRIMARY KEY AUTOINCREMENT,SID TEXT,DID INTEGER,ShiftID INTEGER,Status TEXT DEFAULT 'ready',QueuedAt TEXT,LoadedAt TEXT,ReturnedAt TEXT,WithdrawnAt TEXT,LoadedBy TEXT,ReturnedBy TEXT,CashDue REAL DEFAULT 0,CashCollected REAL,ReturnReason TEXT,UNIQUE(SID)",
}
# v2: أعمدة جديدة في Sales (بتتضاف تلقائياً لقاعدة بيانات قديمة)
SALES_NEW = {'Void': 'INTEGER DEFAULT 0', 'VReason': 'TEXT', 'VBy': 'TEXT', 'VAt': 'TEXT',
             'EBy': 'TEXT', 'EAt': 'TEXT', 'ECount': 'INTEGER DEFAULT 0', 'Comment': 'TEXT', 'Returned': 'INTEGER DEFAULT 0', 'Branch': 'TEXT'}


def hp(u, p):
    return hashlib.sha256(('istakoza|' + u.lower() + '|' + p).encode('utf-8')).hexdigest()


@contextmanager
def db():
    c = sqlite3.connect(DB, timeout=30)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        for n, d in T.items():
            c.execute('CREATE TABLE IF NOT EXISTS %s (%s)' % (n, d))
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(Products)')}
        if 'hidden' not in have:
            c.execute('ALTER TABLE Products ADD COLUMN Hidden INTEGER DEFAULT 0')
        if 'code' not in have:
            c.execute('ALTER TABLE Products ADD COLUMN Code TEXT')
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(Sales)')}
        for col, typ in SALES_NEW.items():
            if col.lower() not in have:
                c.execute('ALTER TABLE Sales ADD COLUMN %s %s' % (col, typ))
        # v6: حالة الطلبية (جاري التنفيذ / تم الاستلام / ملغاة) + الكمية المستلمة فعلاً
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(POrders)')}
        for col, typ in (('Stage', "TEXT DEFAULT 'progress'"), ('RecvAt', 'TEXT'), ('RecvBy', 'TEXT')):
            if col.lower() not in have:
                c.execute('ALTER TABLE POrders ADD COLUMN %s %s' % (col, typ))
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(POItems)')}
        if 'rqty' not in have:
            c.execute('ALTER TABLE POItems ADD COLUMN RQty REAL')
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(Products)')}
        if 'cost' not in have:
            c.execute('ALTER TABLE Products ADD COLUMN Cost REAL DEFAULT 0')
        have = {r[1].lower() for r in c.execute('PRAGMA table_info(StoreItems)')}
        if 'src' not in have:
            c.execute('ALTER TABLE StoreItems ADD COLUMN Src TEXT')
        c.execute('CREATE INDEX IF NOT EXISTS ix_items_sid ON SaleItems(SID)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_sales_day ON Sales(SDay)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_log_sid ON SaleLog(SID)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_driver_orders_status ON DriverOrders(Status)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_driver_orders_did ON DriverOrders(DID)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_driver_shifts_did ON DriverShifts(DID)')
        for uu, fn in c.execute("SELECT UName,FullName FROM Users WHERE Role='driver'").fetchall():
            c.execute('INSERT OR IGNORE INTO Drivers(UName,FullName,CreatedAt) VALUES (?,?,?)',(uu,fn,now_s()))
        for (sid,) in c.execute("SELECT SID FROM Sales WHERE SType='delivery' AND Void=0").fetchall():
            c.execute("INSERT OR IGNORE INTO DriverOrders(SID,Status,QueuedAt,CashDue) SELECT SID,'ready',SDay||' '||STime,STotal FROM Sales WHERE SID=?",(sid,))
        # أيام اتقفلت قبل التحديث ده: نسجّلها في day_locks
        c.execute("INSERT OR IGNORE INTO day_locks (day,locked_at,locked_by) SELECT DISTINCT SDay,?,'migration' FROM Sales WHERE Closed=1", (now_s(),))
        if c.execute('SELECT COUNT(*) FROM Users').fetchone()[0] == 0:
            c.execute('INSERT INTO Users VALUES (?,?,?,?)', ('admin', hp('admin', 'admin'), 'admin', 'المدير'))


def get_cfg(c):
    row = c.execute("SELECT V FROM Settings WHERE K='cfg'").fetchone()
    try:
        return json.loads(row[0]) if row else {}
    except Exception:
        return {}


# ★★★ [EDIT-MAP] الصلاحيات الافتراضية للكاشير ★★★
PERM_DEF = {'return': 1, 'reprint': 1, 'price': 1, 'del': 1, 'disc': 1}


def can(user, perm):
    """المدير يقدر على كل حاجة، والكاشير بالصلاحيات اللي المدير إداها له (cfg.perm)"""
    if user['role'] == 'admin':
        return True
    with db() as c:
        return bool((get_cfg(c).get('perm') or {}).get(perm, PERM_DEF.get(perm, 0)))


SALE_COLS = ('SID,SNo,SDay,STime,SHour,SType,Cashier,CName,CPhone,CAddr,SSub,SDisc,STax,SFee,STotal,Closed,'
             'Void,VReason,VBy,VAt,EBy,EAt,ECount,Comment,Returned,Branch')


def row_to_sale(r, items):
    return dict(id=r[0], no=r[1], day=r[2], time=r[3], h=r[4], type=r[5], by=r[6],
                sub=r[10], disc=r[11], tax=r[12], fee=r[13], total=r[14], items=items,
                cust=dict(name=r[7], phone=r[8], addr=r[9]) if r[5] == 'delivery' else None,
                void=int(r[16] or 0), vreason=r[17] or '', vby=r[18] or '', vat=r[19] or '',
                eby=r[20] or '', eat=r[21] or '', ecount=int(r[22] or 0), comment=r[23] or '', returned=int(r[24] or 0), branch=r[25] or '')


# ★★★ [EDIT-MAP] اللي بيتبعت للصفحة (أصناف، فواتير، ريسبي...) ★★★
def get_state():
    with db() as c:
        prods = [dict(id=r[0], name=r[1], cat=r[2], unit=r[3], price=r[4], icon=r[5], code=r[6], hidden=bool(r[7]), cost=r[8] or 0)
                 for r in c.execute('SELECT PID,PName,Cat,Unit,Price,Icon,Code,Hidden,Cost FROM Products ORDER BY PID')]
        items = {}
        for r in c.execute('SELECT SID,PID,IName,Cat,Unit,Qty,Price FROM SaleItems'):
            items.setdefault(r[0], []).append(dict(id=r[1], name=r[2], cat=r[3], unit=r[4], qty=r[5], price=r[6]))
        locked = {r[0] for r in c.execute('SELECT day FROM day_locks')}
        S, A = [], []
        for r in c.execute('SELECT %s FROM Sales ORDER BY SDay,SID' % SALE_COLS):
            sl = row_to_sale(r, items.get(r[0], []))
            sl['locked'] = r[2] in locked
            (A if r[15] else S).append(sl)
        stock = {str(r[0]): r[1] for r in c.execute('SELECT PID,Qty FROM Stock')}
        sync_fish(c)
        seed_prices(c)
        rc, cs, ad = mat_move(c, '0000-00-00', '9999-99-99', closed_only=True)
        mbase = {}
        for (code,) in c.execute('SELECT Code FROM StoreItems').fetchall():
            k = str(code)
            mbase[k] = round(rc.get(k, 0) - cs.get(k, 0) + ad.get(k, 0), 3)
        recipes = {}
        for pid, code, q in c.execute('SELECT PID,Code,Qty FROM Recipe ORDER BY rowid'):
            recipes.setdefault(str(pid), []).append(dict(code=str(code), qty=q))
        return dict(products=prods, sales=S, archive=A, stock=stock, cfg=get_cfg(c), ver=VERSION, returns=list_returns(c),
                    recipes=recipes, mbase=mbase)


# ★★★ [EDIT-MAP] حفظ الأصناف والأسعار والإعدادات (فحص الصلاحيات هنا) ★★★
def save_state(d, role):
    with LOCK, db() as c:
        if role == 'admin':
            c.execute('DELETE FROM Products')
            c.executemany('INSERT INTO Products (PID,PName,Cat,Unit,Price,Icon,Code,Hidden,Cost) VALUES (?,?,?,?,?,?,?,?,?)',
                          [(int(p['id']), p['name'], p.get('cat', ''), p.get('unit', 'pc'), float(p.get('price') or 0),
                            p.get('icon', ''), str(p.get('code') or ''), 1 if p.get('hidden') else 0, float(p.get('cost') or 0))
                           for p in d.get('products', [])])
            c.execute('DELETE FROM Stock')
            c.executemany('INSERT INTO Stock VALUES (?,?)', [(int(k), float(v)) for k, v in (d.get('stock') or {}).items()])
            c.execute('DELETE FROM Settings')
            c.execute('INSERT INTO Settings VALUES (?,?)', ('cfg', json.dumps(d.get('cfg') or {}, ensure_ascii=False)))
        elif role != 'none':
            for p in d.get('products', []):
                c.execute('UPDATE Products SET Price=? WHERE PID=?', (float(p.get('price') or 0), int(p['id'])))


def ins_sale(c, s, closed=0):
    cu = s.get('cust') or {}
    c.execute('INSERT INTO Sales (SID,SNo,SDay,STime,SHour,SType,Cashier,CName,CPhone,CAddr,SSub,SDisc,STax,SFee,STotal,Closed,'
              'Void,VReason,VBy,VAt,EBy,EAt,ECount,Comment,Returned,Branch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (s['id'], s['no'], s['day'], s['time'], s.get('h', 0), s.get('type', 'cash'), s.get('by', ''),
               cu.get('name', ''), cu.get('phone', ''), cu.get('addr', ''), s['sub'], s['disc'], s.get('tax', 0),
               s.get('fee', 0), s['total'], closed, 1 if s.get('void') else 0, s.get('vreason', ''), s.get('vby', ''),
               s.get('vat', ''), s.get('eby', ''), s.get('eat', ''), s.get('ecount', 0), s.get('comment', ''), s.get('returned', 0), s.get('branch', '')))
    put_items(c, s['id'], s['items'])


def put_items(c, sid, items):
    c.executemany('INSERT INTO SaleItems VALUES (?,?,?,?,?,?,?)',
                  [(sid, i['id'], i['name'], i.get('cat') or '', i['unit'], i['qty'], i['price']) for i in items])


# ★★★ [EDIT-MAP] حفظ الفاتورة: الترقيم + الوقت من السيرفر + خصم الخامات ★★★
def add_sale(d, user):
    # الترقيم بيتم هنا جوه السيرفر (داخل قفل) عشان جهازين مايطلعوش نفس رقم الفاتورة
    with LOCK, db() as c:
        ex = c.execute('SELECT SNo FROM Sales WHERE SID=?', (d['id'],)).fetchone()
        if ex:
            return {'ok': 1, 'no': ex[0], 'id': d['id'], 'dup': 1}
        d['no'] = (c.execute('SELECT MAX(SNo) FROM Sales WHERE Closed=0').fetchone()[0] or 0) + 1
        _n = datetime.now()
        d['day'], d['time'], d['h'] = _n.strftime('%Y-%m-%d'), _n.strftime('%H:%M:%S'), _n.hour
        d['by'] = user['u']
        d['branch'] = str(get_cfg(c).get('branchCode') or '')
        ins_sale(c, d)
        if d.get('type') == 'delivery': c.execute('INSERT OR IGNORE INTO DriverOrders(SID,Status,QueuedAt,CashDue) VALUES (?,?,?,?)',(d['id'],'ready',now_s(),d.get('total',0)))
    return {'ok': 1, 'no': d['no'], 'id': d['id']}


# ------------------------------------------------------------------ void / edit (v2) + قفل الأيام (v3)
LOCK_MSG = '🚫 غير مسموح! هذا اليوم تم تقفيله رسمياً. استخدم شاشة المرتجع بتاريخ اليوم.'


class Forbidden(Exception):
    pass


def check_unlocked(c, day):
    if c.execute('SELECT 1 FROM day_locks WHERE day=?', (day,)).fetchone():
        raise Forbidden(LOCK_MSG)
def log(c, sid, act, user, reason, detail):
    c.execute('INSERT INTO SaleLog (SID,Act,ByUser,Reason,At,Detail) VALUES (?,?,?,?,?,?)',
              (sid, act, user['u'], reason, now_s(), json.dumps(detail, ensure_ascii=False)))


def snapshot(c, sid):
    r = c.execute('SELECT %s FROM Sales WHERE SID=?' % SALE_COLS, (sid,)).fetchone()
    if not r:
        raise Exception('الأوردر غير موجود')
    items = [dict(id=i[0], name=i[1], cat=i[2], unit=i[3], qty=i[4], price=i[5])
             for i in c.execute('SELECT PID,IName,Cat,Unit,Qty,Price FROM SaleItems WHERE SID=?', (sid,))]
    return row_to_sale(r, items), bool(r[15])


def stock_back(c, old_items, new_items):
    """أوردر من يوم اتقفل: المخزن اتخصم منه وقت القفل، فلازم نرجّع الفرق (المخزن للأصناف اللي ليها رصيد بس)"""
    delta = {}
    for i in old_items:
        delta[i['id']] = delta.get(i['id'], 0) + i['qty']
    for i in new_items:
        delta[i['id']] = delta.get(i['id'], 0) - i['qty']
    for pid, q in delta.items():
        if abs(q) > 1e-9:
            c.execute('UPDATE Stock SET Qty=Qty+? WHERE PID=?', (round(q, 3), pid))


# ★★★ [EDIT-MAP] إلغاء أوردر + قفل الأيام ★★★
def do_void(d, user):
    reason = str(d.get('reason', '')).strip()
    if not reason:
        raise Exception('اكتب سبب الإلغاء')
    with LOCK, db() as c:
        s, closed = snapshot(c, d['id'])
        check_unlocked(c, s['day'])
        if s['returned']:
            raise Exception('الأوردر عليه مرتجع - مينفعش يتلغي')
        if s['void']:
            raise Exception('الأوردر ملغي بالفعل')
        c.execute('UPDATE Sales SET Void=1,VReason=?,VBy=?,VAt=? WHERE SID=?', (reason, user['u'], now_s(), d['id']))
        if closed:
            stock_back(c, s['items'], [])
        log(c, d['id'], 'void', user, reason, {'before': s})


# ★★★ [EDIT-MAP] تعديل أوردر ★★★
def do_edit(d, user):
    reason = str(d.get('reason', '')).strip()
    if not reason:
        raise Exception('اكتب سبب التعديل')
    items = []
    for i in d.get('items') or []:
        q, p = float(i['qty']), float(i['price'])
        if q <= 0 or p < 0:
            raise Exception('كمية أو سعر غير صحيح')
        items.append(dict(id=int(i['id']), name=str(i['name']), cat=str(i.get('cat') or ''),
                          unit='kg' if i.get('unit') == 'kg' else 'pc', qty=round(q, 3), price=p))
    if not items:
        raise Exception('الأوردر لازم يفضل فيه صنف واحد على الأقل (للإلغاء استخدم Void)')
    with LOCK, db() as c:
        s, closed = snapshot(c, d['id'])
        check_unlocked(c, s['day'])
        if s['returned']:
            raise Exception('الأوردر عليه مرتجع - مينفعش يتعدل')
        if s['void']:
            raise Exception('الأوردر ملغي - مينفعش يتعدل')
        cu = d.get('cust') or {}
        if s['type'] == 'delivery':
            cn, cp, ca = cu.get('name', ''), cu.get('phone', ''), cu.get('addr', '')
        else:
            cn, cp, ca = '', '', ''
        c.execute('UPDATE Sales SET SSub=?,SDisc=?,STax=?,SFee=?,STotal=?,CName=?,CPhone=?,CAddr=?,EBy=?,EAt=?,ECount=ECount+1,Comment=? WHERE SID=?',
                  (float(d['sub']), float(d['disc']), float(d.get('tax', 0)), float(d.get('fee', 0)), float(d['total']),
                   cn, cp, ca, user['u'], now_s(), str(d.get('comment', s['comment']) or '').strip(), d['id']))
        c.execute('DELETE FROM SaleItems WHERE SID=?', (d['id'],))
        put_items(c, d['id'], items)
        if closed:
            stock_back(c, s['items'], items)
        after, _ = snapshot(c, d['id'])
        log(c, d['id'], 'edit', user, reason, {'before': s, 'after': after})


def get_log(sid):
    with db() as c:
        return [dict(act=r[0], by=r[1], reason=r[2], at=r[3])
                for r in c.execute('SELECT Act,ByUser,Reason,At FROM SaleLog WHERE SID=? ORDER BY LID', (sid,))]



# ------------------------------------------------------------------ delivery drivers v11
def db_driver_list():
    with db() as c:
        return [dict(DID=r[0],UName=r[1]) for r in c.execute('SELECT DID,UName FROM Drivers WHERE Active=1')]

def sale_json(c,sid):
    r=c.execute('SELECT %s FROM Sales WHERE SID=?' % SALE_COLS,(sid,)).fetchone()
    if not r:return None
    items=[dict(id=i[0],name=i[1],cat=i[2],unit=i[3],qty=i[4],price=i[5]) for i in c.execute('SELECT PID,IName,Cat,Unit,Qty,Price FROM SaleItems WHERE SID=?',(sid,))]
    return row_to_sale(r,items)

def delivery_get(user):
    with db() as c:
        drivers=[dict(did=r[0],u=r[1],name=r[2],active=bool(r[3])) for r in c.execute('SELECT DID,UName,FullName,Active FROM Drivers ORDER BY Active DESC,FullName')]
        if user['role']=='driver': drivers=[x for x in drivers if x['u']==user['u']]
        only_did=drivers[0]['did'] if user['role']=='driver' and drivers else None
        rows=[]
        q="""SELECT s.SID,s.SNo,s.SDay,s.STime,s.STotal,s.CName,s.CPhone,s.CAddr,o.DOID,o.DID,o.ShiftID,o.Status,o.QueuedAt,o.LoadedAt,o.ReturnedAt,o.CashDue,o.CashCollected,o.ReturnReason
             FROM Sales s JOIN DriverOrders o ON o.SID=s.SID WHERE s.SType='delivery' AND s.Void=0 AND o.Status IN ('ready','loaded','returned')
             ORDER BY CASE o.Status WHEN 'ready' THEN 0 WHEN 'loaded' THEN 1 ELSE 2 END,o.QueuedAt"""
        if only_did is not None: q=q.replace(" WHERE s.SType='delivery'"," WHERE s.SType='delivery' AND o.DID=%d" % only_did)
        for r in c.execute(q):
            rows.append(dict(id=r[0],no=r[1],day=r[2],time=r[3],total=r[4],cust={'name':r[5] or '','phone':r[6] or '','addr':r[7] or ''},doid=r[8],did=r[9],shift_id=r[10],status=r[11],queued_at=r[12],loaded_at=r[13],returned_at=r[14],cash_due=r[15] or 0,cash_collected=r[16],return_reason=r[17] or '',sale=sale_json(c,r[0])))
        oq="SELECT o.DID,o.ShiftID,d.FullName,o.SID,o.DOID,o.Status,o.LoadedAt,s.SNo,s.STotal FROM DriverOrders o JOIN Drivers d ON d.DID=o.DID JOIN Sales s ON s.SID=o.SID WHERE o.Status='loaded'" + ((" AND o.DID=%d" % only_did) if only_did is not None else '') + " ORDER BY o.LoadedAt"
        outside=[dict(did=r[0],shift_id=r[1],driver=r[2],sid=r[3],doid=r[4],status=r[5],loaded_at=r[6],no=r[7],total=r[8]) for r in c.execute(oq)]
        sq="SELECT sh.ShiftID,sh.DID,d.FullName,sh.OpenedAt,sh.ClosedAt,sh.OpeningCash,sh.ClosingCash,sh.ExpectedCash,sh.Status,sh.OpenedBy,sh.ClosedBy FROM DriverShifts sh JOIN Drivers d ON d.DID=sh.DID WHERE sh.Status='open'" + ((" AND sh.DID=%d" % only_did) if only_did is not None else '') + " ORDER BY d.FullName"
        shifts=[dict(shift_id=r[0],did=r[1],driver=r[2],opened_at=r[3],closed_at=r[4],opening_cash=r[5] or 0,closing_cash=r[6],expected_cash=r[7] or 0,status=r[8],opened_by=r[9],closed_by=r[10]) for r in c.execute(sq)]
        return {'drivers':drivers,'orders':rows,'outside':outside,'shifts':shifts,'me_driver':next((x for x in drivers if x['u']==user['u']),None)}

def delivery_open_shift(d,user):
    did=int(d.get('did') or 0); cash=float(d.get('opening_cash') or 0)
    with LOCK,db() as c:
        if not c.execute('SELECT DID FROM Drivers WHERE DID=? AND Active=1',(did,)).fetchone(): raise Exception('المندوب غير موجود أو غير مفعل')
        if c.execute("SELECT 1 FROM DriverShifts WHERE DID=? AND Status='open'",(did,)).fetchone(): raise Exception('للمندوب وردية مفتوحة بالفعل')
        c.execute("INSERT INTO DriverShifts(DID,OpenedAt,OpeningCash,Status,OpenedBy) VALUES (?,?,?,'open',?)",(did,now_s(),cash,user['u']))

def ensure_driver_order(c,sid):
    if not c.execute('SELECT 1 FROM DriverOrders WHERE SID=?',(sid,)).fetchone():
        s=c.execute('SELECT STotal,SType FROM Sales WHERE SID=?',(sid,)).fetchone()
        if s and s[1]=='delivery': c.execute('INSERT INTO DriverOrders(SID,Status,QueuedAt,CashDue) VALUES (?,?,?,?)',(sid,'ready',now_s(),s[0] or 0))

def delivery_assign(d,user):
    ids=d.get('ids') or []; did=int(d.get('did') or 0)
    if not ids: raise Exception('اختار أوردر واحد على الأقل')
    with LOCK,db() as c:
        if not c.execute('SELECT DID FROM Drivers WHERE DID=? AND Active=1',(did,)).fetchone(): raise Exception('المندوب غير موجود')
        sh=c.execute("SELECT ShiftID FROM DriverShifts WHERE DID=? AND Status='open'",(did,)).fetchone()
        if not sh: raise Exception('افتح وردية المندوب أولاً')
        for sid in ids:
            s=c.execute('SELECT STotal,SType,Void FROM Sales WHERE SID=?',(sid,)).fetchone()
            if not s or s[1]!='delivery' or s[2]: continue
            ensure_driver_order(c,sid)
            o=c.execute('SELECT Status FROM DriverOrders WHERE SID=?',(sid,)).fetchone()
            if not o or o[0] not in ('ready','returned'): raise Exception('أحد الأوردرات ليس جاهزاً للتحميل')
            c.execute("UPDATE DriverOrders SET DID=?,ShiftID=?,Status='loaded',LoadedAt=?,LoadedBy=?,CashDue=?,CashCollected=NULL,ReturnReason=NULL WHERE SID=?",(did,sh[0],now_s(),user['u'],s[0] or 0,sid))
            log(c,sid,'driver_load',user,'تحميل مندوب',{'did':did,'shift':sh[0]})

def delivery_withdraw(d,user):
    sid=str(d.get('sid'))
    with LOCK,db() as c:
        o=c.execute('SELECT Status FROM DriverOrders WHERE SID=?',(sid,)).fetchone()
        if not o or o[0]!='ready': raise Exception('الأوردر غير موجود في قائمة الجاهز')
        c.execute("UPDATE DriverOrders SET Status='withdrawn',WithdrawnAt=? WHERE SID=?",(now_s(),sid)); log(c,sid,'driver_withdraw',user,'سحب من قائمة المندوب',{})

def delivery_return_one(d,user):
    sid=str(d.get('sid')); reason=str(d.get('reason') or 'رجوع أوردر منفرد').strip()
    with LOCK,db() as c:
        o=c.execute("SELECT Status,DID,ShiftID FROM DriverOrders WHERE SID=?",(sid,)).fetchone()
        if not o or o[0]!='loaded': raise Exception('الأوردر ليس بالخارج')
        c.execute("UPDATE DriverOrders SET Status='returned',ReturnedAt=?,ReturnedBy=?,ReturnReason=? WHERE SID=?",(now_s(),user['u'],reason,sid))
        log(c,sid,'driver_return',user,reason,{'did':o[1],'shift':o[2]})

def delivery_return_all(d,user):
    did=int(d.get('did') or 0); reason=str(d.get('reason') or 'رجوع المندوب').strip()
    with LOCK,db() as c:
        sh=c.execute("SELECT ShiftID FROM DriverShifts WHERE DID=? AND Status='open'",(did,)).fetchone()
        if not sh: raise Exception('لا توجد وردية مفتوحة')
        for (sid,) in c.execute("SELECT SID FROM DriverOrders WHERE DID=? AND ShiftID=? AND Status='loaded'",(did,sh[0])).fetchall():
            c.execute("UPDATE DriverOrders SET Status='returned',ReturnedAt=?,ReturnedBy=?,ReturnReason=? WHERE SID=?",(now_s(),user['u'],reason,sid)); log(c,sid,'driver_return',user,reason,{'did':did,'shift':sh[0]})

def delivery_close_shift(d,user):
    did=int(d.get('did') or 0); closing=float(d.get('closing_cash') or 0)
    with LOCK,db() as c:
        sh=c.execute("SELECT ShiftID,OpeningCash FROM DriverShifts WHERE DID=? AND Status='open'",(did,)).fetchone()
        if not sh: raise Exception('لا توجد وردية مفتوحة')
        if c.execute("SELECT COUNT(*) FROM DriverOrders WHERE DID=? AND ShiftID=? AND Status='loaded'",(did,sh[0])).fetchone()[0]: raise Exception('لا يمكن قفل الشفت: يوجد أوردرات مع المندوب. اضغط إعادة المندوب أولاً.')
        expected=c.execute("SELECT COALESCE(SUM(CashCollected),0) FROM DriverOrders WHERE DID=? AND ShiftID=? AND CashCollected IS NOT NULL",(did,sh[0])).fetchone()[0]
        c.execute("UPDATE DriverShifts SET ClosedAt=?,ClosingCash=?,ExpectedCash=?,Status='closed',ClosedBy=? WHERE ShiftID=?",(now_s(),closing,expected,user['u'],sh[0]))
        return {'expected':expected,'closing_cash':closing,'difference':round(closing-expected-(sh[1] or 0),2)}

def delivery_collect(d,user):
    sid=str(d.get('sid')); cash=float(d.get('cash') if d.get('cash') is not None else 0)
    with LOCK,db() as c:
        o=c.execute("SELECT Status FROM DriverOrders WHERE SID=?",(sid,)).fetchone()
        if not o or o[0]!='loaded': raise Exception('الأوردر ليس مع المندوب')
        c.execute('UPDATE DriverOrders SET CashCollected=? WHERE SID=?',(cash,sid)); log(c,sid,'driver_cash',user,'تحصيل كاش المندوب',{'cash':cash})

# ------------------------------------------------------------------ misc
# ★★★ [EDIT-MAP] الطباعة على طابعة الشبكة ★★★
def do_print(d):
    """يبعت بيانات ESC/POS خام للطابعة على الشبكة (بورت 9100)"""
    ip = str(d.get('ip', '')).strip()
    port = int(d.get('port') or 9100)
    try:
        a = ipaddress.ip_address(ip)
    except Exception:
        raise Exception('عنوان IP غير صحيح')
    if not (a.is_private or a.is_loopback):
        raise Exception('الـ IP لازم يكون على الشبكة المحلية')
    try:
        data = base64.b64decode(d.get('data', ''))
    except Exception:
        raise Exception('بيانات الطباعة غير صالحة')
    try:
        with socket.create_connection((ip, port), timeout=5) as s:
            s.settimeout(10)
            s.sendall(data)
    except Exception as e:
        raise Exception('تعذر الاتصال بالطابعة %s:%d (%s)' % (ip, port, e))


def do_import(d):
    save_state(d, 'admin')
    with LOCK, db() as c:
        c.execute('DELETE FROM Sales')
        c.execute('DELETE FROM SaleItems')
        for closed, lst in ((1, d.get('archive', [])), (0, d.get('sales', []))):
            for k, s in enumerate(lst):
                s['id'] = s.get('id') or '%s-%s-%s-%d' % (s['day'], s['no'], closed, k)
                ins_sale(c, s, closed)
        c.execute('DELETE FROM day_locks')
        c.execute("INSERT OR IGNORE INTO day_locks (day,locked_at,locked_by) SELECT DISTINCT SDay,?,'import' FROM Sales WHERE Closed=1", (now_s(),))


def reset():
    with LOCK, db() as c:
        for t in ('Sales', 'SaleItems', 'Stock', 'SaleLog', 'day_locks', 'Returns', 'ReturnItems', 'Stocktake', 'StocktakeMeta', 'StoreAdj', 'StockCount', 'DriverOrders', 'DriverShifts'):
            c.execute('DELETE FROM ' + t)


def login(u, p):
    with db() as c:
        r = c.execute('SELECT UName,Role,FullName FROM Users WHERE UName=? AND PHash=?', (u, hp(u, p))).fetchone()
    return {'u': r[0], 'role': r[1], 'full': r[2]} if r else None


def list_users():
    with db() as c:
        return [dict(u=r[0], role=r[1], full=r[2]) for r in c.execute('SELECT UName,Role,FullName FROM Users')]


def save_user(d):
    u = d['u'].strip()
    if not re.fullmatch(r'[\w.-]+', u):
        raise Exception('اسم الدخول: حروف وأرقام بدون مسافات')
    with LOCK, db() as c:
        ex = c.execute('SELECT Role FROM Users WHERE UName=?', (u,)).fetchone()
        if d.get('del'):
            if ex and ex[0] == 'admin' and c.execute("SELECT COUNT(*) FROM Users WHERE Role='admin'").fetchone()[0] <= 1:
                raise Exception('لا يمكن حذف آخر مدير')
            c.execute('DELETE FROM Users WHERE UName=?', (u,))
            c.execute('DELETE FROM Drivers WHERE UName=?', (u,))
        elif ex:
            if d.get('p'):
                c.execute('UPDATE Users SET PHash=? WHERE UName=?', (hp(u, d['p']), u))
        else:
            if not d.get('p'):
                raise Exception('أدخل كلمة السر')
            role=d.get('role', 'cashier')
            if role not in ('admin','cashier','driver'): raise Exception('نوع مستخدم غير صحيح')
            c.execute('INSERT INTO Users VALUES (?,?,?,?)', (u, hp(u, d['p']), role, d.get('full', '')))
            if role == 'driver': c.execute('INSERT OR IGNORE INTO Drivers(UName,FullName,CreatedAt) VALUES (?,?,?)',(u,d.get('full',''),now_s()))


# ------------------------------------------------------------------ مرتجع (v4) - 4 أقفال
# 1) بيختار فاتورة حقيقية من السيستم والأصناف بتيجي منها  2) كل صنف يرجع مرة واحدة بس (بالكمية)
# 3) باسورد مدير + سبب من قايمة مقفولة  4) فلوس المرتجع "مديونية مرتجع" مش من الدرج: المدير يصرفها من خزنة منفصلة
RET_REASONS_DEF = ['أوردر غلط', 'جودة سيئة', 'الزبون لغى', 'تأخير في التوصيل', 'صنف ناقص', 'أخرى']
FAILS = {}


def ret_reasons(cfg):
    r = [x.strip() for x in re.split(r'[,،]', str(cfg.get('retReasons') or '')) if x.strip()]
    return r or RET_REASONS_DEF


def find_manager(c, pw):
    for u, full, h in c.execute("SELECT UName,FullName,PHash FROM Users WHERE Role='admin'").fetchall():
        if hp(u, pw) == h:
            return u, (full or u)
    return None


def full_name(c, u):
    r = c.execute('SELECT FullName FROM Users WHERE UName=?', (u,)).fetchone()
    return r[0] if r and r[0] else u


def ret_dict(c, r):
    items = [dict(id=i[0], name=i[1], cat=i[2], unit=i[3], qty=i[4], price=i[5], refund=i[6])
             for i in c.execute('SELECT PID,IName,Cat,Unit,Qty,Price,Refund FROM ReturnItems WHERE RID=?', (r[0],))]
    return dict(id=r[0], no=r[1], sid=r[2], sno=r[3], day=r[4], time=r[5], reason=r[6], by=r[7], mgr=r[8],
                total=r[9], status=r[10], paidBy=r[11] or '', paidAt=r[12] or '', log=r[13] or '', items=items)


RET_COLS = 'RID,RNo,SID,SNo,RDay,RTime,Reason,Cashier,Manager,Total,Status,PaidBy,PaidAt,Log'


def list_returns(c):
    return [ret_dict(c, r) for r in c.execute('SELECT %s FROM Returns ORDER BY RNo' % RET_COLS).fetchall()]


# ★★★ [EDIT-MAP] المرتجع بالأقفال الأربعة ★★★
def do_return(d, user):
    now = datetime.now()
    key = user['u']
    fl = [t for t in FAILS.get(key, []) if (now - t).total_seconds() < 600]
    if len(fl) >= 5:
        raise Forbidden('تم إيقاف المرتجع مؤقتاً بسبب باسورد مدير غلط كذا مرة - استنى 10 دقايق')
    with LOCK, db() as c:
        cfg = get_cfg(c)
        reason = str(d.get('reason', '')).strip()
        if reason not in ret_reasons(cfg):
            raise Exception('اختار سبب المرتجع من القايمة')
        mgr = find_manager(c, str(d.get('pw', '')))
        if not mgr:
            fl.append(now)
            FAILS[key] = fl
            c.commit()
            raise Forbidden('باسورد المدير غير صحيح')
        FAILS.pop(key, None)
        s, closed = snapshot(c, d['sid'])
        if s['void']:
            raise Exception('الأوردر ملغي - مينفعش يتعمله مرتجع')
        orig, wsum = {}, {}
        for i in s['items']:
            orig[i['id']] = orig.get(i['id'], 0) + i['qty']
            wsum[i['id']] = wsum.get(i['id'], 0) + i['qty'] * i['price']
        meta = {i['id']: i for i in s['items']}
        done = {r[0]: r[1] for r in c.execute(
            'SELECT ri.PID,SUM(ri.Qty) FROM ReturnItems ri JOIN Returns r ON r.RID=ri.RID WHERE r.SID=? GROUP BY ri.PID', (s['id'],))}
        factor = (s['total'] - s['fee']) / s['sub'] if s['sub'] > 0 else 1.0
        lines, total = [], 0.0
        for it in d.get('items') or []:
            pid, q = int(it['id']), round(float(it['qty']), 3)
            if q <= 0:
                continue
            if pid not in orig:
                raise Forbidden('الصنف ده مش موجود في الفاتورة الأصلية')
            if done.get(pid, 0) + q > orig[pid] + 1e-9:
                raise Forbidden('الكمية دي رجعت قبل كده')
            price = wsum[pid] / orig[pid]
            refund = round(q * price * factor, 2)
            m = meta[pid]
            lines.append((pid, m['name'], m.get('cat') or '', m['unit'], q, round(price, 4), refund))
            total += refund
            done[pid] = done.get(pid, 0) + q
        if not lines:
            raise Exception('اختار كمية للمرتجع')
        rno = (c.execute('SELECT MAX(RNo) FROM Returns').fetchone()[0] or 0) + 1
        rid = secrets.token_hex(8)
        cname = full_name(c, user['u'])
        text = 'مرتجع فاتورة %s بواسطة كاشير %s وافق عليه مدير %s الساعة %s' % (s['no'], cname, mgr[1], now.strftime('%H:%M'))
        c.execute('INSERT INTO Returns (%s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)' % RET_COLS,
                  (rid, rno, s['id'], s['no'], now.strftime('%Y-%m-%d'), now.strftime('%H:%M'), reason, cname, mgr[1],
                   round(total, 2), 'due', '', '', text))
        c.executemany('INSERT INTO ReturnItems VALUES (?,?,?,?,?,?,?,?)', [(rid,) + l for l in lines])
        full = all(done.get(pid, 0) >= orig[pid] - 1e-9 for pid in orig)
        c.execute('UPDATE Sales SET Returned=? WHERE SID=?', (2 if full else 1, s['id']))
        log(c, s['id'], 'return', user, text, {'rid': rid, 'total': round(total, 2), 'reason': reason})
        r = c.execute('SELECT %s FROM Returns WHERE RID=?' % RET_COLS, (rid,)).fetchone()
        return ret_dict(c, r)


def do_return_pay(d, user):
    with LOCK, db() as c:
        c.execute("UPDATE Returns SET Status='paid',PaidBy=?,PaidAt=? WHERE RID=? AND Status='due'",
                  (full_name(c, user['u']), now_s(), d['rid']))


# ------------------------------------------------------------------ طلبيات الشراء + الموردين + الأكواد المخزنية (v4)
def po_dict(c, r):
    items = [dict(code=i[0], name=i[1], unit=i[2], qty=i[3], price=i[4], rqty=i[5])
             for i in c.execute('SELECT Code,IName,Unit,Qty,Price,RQty FROM POItems WHERE POID=?', (r[0],))]
    return dict(id=r[0], no=r[1], day=r[2], time=r[3], supplier=r[4], total=r[5], status=r[6], by=r[7],
                sentAt=r[8] or '', note=r[9] or '', err=r[10] or '', stage=r[11] or 'progress',
                recvAt=r[12] or '', recvBy=r[13] or '', items=items)


PO_BASE = 'POID,PONo,PODay,POTime,Supplier,Total,Status,CreatedBy,SentAt,Note,Err'
PO_COLS = PO_BASE + ',Stage,RecvAt,RecvBy'


FISH_CAT = 'أسماك'


# أسعار تقديرية (جنيه/كيلو) من أسواق مصر أكتوبر 2026: سوق العبور (الأسماك) وبوابة الأسعار (السلع) - عدّلها من 🏷️ الموردين والأكواد
# ★★★ [EDIT-MAP] ⭐ أسعار الأسماك التقديرية (أكتوبر 2026) - عدّلها هنا أو من شاشة الأكواد ★★★
FISH_RULES = [('كفتة', 0), ('فيليه', 150), ('قزاز', 300), ('جمبري', 580), ('بلطي', 80), ('مكريل', 120), ('ماكريل', 120), ('مكرونة', 150), ('وقار', 220),
              ('دينيس', 300), ('قشر بياض', 250), ('بربوني', 200), ('بريوني', 200), ('موسى', 330), ('قاروص', 250), ('لوت', 250), ('شعور', 200),
              ('ثعابين', 250), ('كابوريا', 180), ('سبيط', 300), ('كاليماري', 300)]
# ★★★ [EDIT-MAP] ⭐ الخامات الأساسية وأسعارها (ملح، زيت، أرز...) ★★★
STAPLES = [('5001', 'أرز', 'kg', 'بقالة', 35), ('5002', 'دقيق', 'kg', 'بقالة', 27), ('5003', 'زيت عباد الشمس', 'ltr', 'بقالة', 105),
           ('5004', 'زيت ذرة', 'ltr', 'بقالة', 120), ('5005', 'سكر', 'kg', 'بقالة', 35), ('5006', 'مكرونة', 'kg', 'بقالة', 26),
           ('5007', 'طماطم', 'kg', 'خضار', 20), ('5008', 'بصل', 'kg', 'خضار', 17), ('5009', 'ثوم', 'kg', 'خضار', 56),
           ('5010', 'ليمون', 'kg', 'خضار', 27), ('5011', 'بطاطس', 'kg', 'خضار', 20), ('5012', 'بيض', 'pc', 'بقالة', 4.2),
           ('5013', 'فراخ', 'kg', 'لحوم', 95), ('5014', 'كمون', 'kg', 'بهارات', 300), ('5015', 'فلفل أسود', 'kg', 'بهارات', 420),
           ('5016', 'شطة', 'kg', 'بهارات', 215), ('5017', 'كزبرة ناشفة', 'kg', 'بهارات', 140), ('5018', 'بابريكا', 'kg', 'بهارات', 170),
           ('5019', 'ملح', 'kg', 'بقالة', 0), ('5020', 'طحينة', 'kg', 'بقالة', 0)]


def fish_price(name):
    for k, v in FISH_RULES:
        if k in name:
            return v
    return 0


def seed_prices(c):
    c.execute('CREATE TABLE IF NOT EXISTS Flags (K TEXT PRIMARY KEY)')
    if not c.execute("SELECT 1 FROM Flags WHERE K='staples_v1'").fetchone():
        for code, name, unit, cat, price in STAPLES:
            if not c.execute('SELECT 1 FROM StoreItems WHERE Code=? OR IName=?', (code, name)).fetchone():
                c.execute("INSERT INTO StoreItems (Code,IName,Unit,Cat,Price,SupID,Src) VALUES (?,?,?,?,?,0,'')", (code, name, unit, cat, price))
        c.execute("INSERT INTO Flags VALUES ('staples_v1')")
    if not c.execute("SELECT 1 FROM Flags WHERE K='fish_prices_v1'").fetchone():
        for code, name in c.execute("SELECT Code,IName FROM StoreItems WHERE Src='menu' AND IFNULL(Price,0)=0").fetchall():
            if fish_price(name):
                c.execute('UPDATE StoreItems SET Price=? WHERE Code=?', (fish_price(name), code))
        c.execute("INSERT INTO Flags VALUES ('fish_prices_v1')")


# ★★★ [EDIT-MAP] مزامنة أصناف جروب الأسماك مع الأكواد المخزنية ★★★
def sync_fish(c):
    """v7: أصناف جروب الأسماك في المنيو بتظهر تلقائياً في أكواد الطلبيات بنفس كود الصنف (الاسم والوحدة بيتحدّثوا، والسعر/المورد بتحدده انت)"""
    cur = set()
    for code, name, unit in c.execute('SELECT Code,PName,Unit FROM Products WHERE Cat=? AND Code IS NOT NULL AND Code<>\'\'', (FISH_CAT,)).fetchall():
        code = str(code).strip()
        cur.add(code)
        if c.execute('SELECT 1 FROM StoreItems WHERE Code=?', (code,)).fetchone():
            c.execute("UPDATE StoreItems SET IName=?,Unit=?,Cat=? WHERE Code=? AND Src='menu'", (name, unit or 'kg', FISH_CAT, code))
        else:
            c.execute('INSERT INTO StoreItems (Code,IName,Unit,Cat,Price,SupID,Src) VALUES (?,?,?,?,?,0,\'menu\')', (code, name, unit or 'kg', FISH_CAT, fish_price(name)))
    # صنف اتمسح أو اتغيّر كوده من المنيو: يتشال من الأكواد (الطلبيات القديمة محتفظة باسمها)
    if cur:  # لو جروب الأسماك اتسمّى تاني/اتمسح: بنوقف المزامنة ومنمسحش حاجة
        for (code,) in c.execute('SELECT Code FROM StoreItems WHERE Src=\'menu\'').fetchall():
            if code in cur:
                continue
            if c.execute('SELECT 1 FROM POItems WHERE Code=? UNION SELECT 1 FROM Recipe WHERE Code=? LIMIT 1', (code, code)).fetchone():
                c.execute("UPDATE StoreItems SET Src='' WHERE Code=?", (code,))  # عليه طلبيات/ريسبي: يفضل كخامة عادية
            else:
                c.execute('DELETE FROM StoreItems WHERE Code=?', (code,))


# ★★★ [EDIT-MAP] بيانات المخازن: الأكواد والموردين والطلبيات ★★★
def get_store():
    with db() as c:
        sync_fish(c)
        seed_prices(c)
        return dict(
            suppliers=[dict(id=r[0], name=r[1], phone=r[2] or '', note=r[3] or '')
                       for r in c.execute('SELECT SupID,SName,Phone,Note FROM Suppliers ORDER BY SName')],
            items=[dict(code=r[0], name=r[1], unit=r[2], cat=r[3] or '', price=r[4] or 0, supid=r[5] or 0)
                   for r in c.execute('SELECT Code,IName,Unit,Cat,Price,SupID FROM StoreItems ORDER BY Code')],
            templates=[dict(id=r[0], name=r[1], items=json.loads(r[2] or '[]'))
                       for r in c.execute('SELECT TID,TName,Items FROM PoTemplates ORDER BY TName')],
            orders=[po_dict(c, r) for r in c.execute('SELECT %s FROM POrders ORDER BY PONo DESC LIMIT 300' % PO_COLS)],
            stock={})


def store_extra(st):
    """رصيد كل خامة دلوقتي + آخر سعر شراء (من آخر طلبية)"""
    with db() as c:
        rc, cs, ad = mat_move(c, '0000-00-00', '9999-99-99')
        pr = last_prices(c)
    for i in st['items']:
        k = str(i['code'])
        i['bal'] = round(rc.get(k, 0) - cs.get(k, 0) + ad.get(k, 0), 3)
        i['last'] = pr.get(k, 0) or 0
    return st


# ★★★ [EDIT-MAP] إنشاء الطلبية ★★★
def do_po(d, user):
    sup = str(d.get('supplier', '')).strip()
    if not sup:
        raise Exception('اكتب اسم المورد')
    with LOCK, db() as c:
        sync_fish(c)
        ex = c.execute('SELECT %s FROM POrders WHERE POID=?' % PO_COLS, (d['id'],)).fetchone()
        if ex:
            return po_dict(c, ex)
        lines, total = [], 0.0
        for it in d.get('items') or []:
            q = round(float(it['qty']), 3)
            row = c.execute('SELECT Code,IName,Unit,Price FROM StoreItems WHERE Code=?', (str(it['code']).strip(),)).fetchone()
            if not row:
                raise Exception('الكود %s غير موجود' % it['code'])
            if q <= 0:
                raise Exception('كمية غير صحيحة للكود %s' % it['code'])
            lines.append((d['id'], row[0], row[1], row[2], q, row[3] or 0))
            total += q * (row[3] or 0)
        if not lines:
            raise Exception('الطلبية فاضية')
        if not c.execute('SELECT 1 FROM Suppliers WHERE SName=?', (sup,)).fetchone():
            c.execute('INSERT INTO Suppliers (SName,Phone,Note) VALUES (?,?,?)', (sup, '', ''))
        n = c.execute("SELECT MAX(CAST(SUBSTR(PONo,4) AS INTEGER)) FROM POrders").fetchone()[0] or 0
        now = datetime.now()
        c.execute('INSERT INTO POrders (%s) VALUES (?,?,?,?,?,?,?,?,?,?,?)' % PO_BASE,
                  (d['id'], 'PO-%04d' % (n + 1), now.strftime('%Y-%m-%d'), now.strftime('%H:%M'), sup, round(total, 2),
                   'saved', user['u'], '', str(d.get('note', '')).strip(), ''))
        c.executemany('INSERT INTO POItems (POID,Code,IName,Unit,Qty,Price) VALUES (?,?,?,?,?,?)', lines)
        return po_dict(c, c.execute('SELECT %s FROM POrders WHERE POID=?' % PO_COLS, (d['id'],)).fetchone())


# ------------------------------------------------------------------ v10: ريسبي + حركة الخامات + جرد الشهر
def last_prices(c):
    """سعر شراء كل خامة = سعرها في آخر طلبية (مش ملغاة)، ولو مفيش بياخد سعر الكود"""
    pr = {str(r[0]): (r[1] or 0) for r in c.execute('SELECT Code,Price FROM StoreItems')}
    for code, price in c.execute("SELECT i.Code,i.Price FROM POItems i JOIN POrders o ON o.POID=i.POID "
                                 "WHERE IFNULL(o.Stage,'progress')!='cancelled' AND IFNULL(i.Price,0)>0 ORDER BY o.PONo"):
        pr[str(code)] = price
    return pr


def usage_map(c):
    """pid -> [(كود الخامة, الكمية لكل وحدة بيع)]: من الريسبي. صنف الأسماك من غير ريسبي وكوده = كود خامة بياخد 1:1"""
    um = {}
    for pid, code, q in c.execute('SELECT PID,Code,Qty FROM Recipe'):
        um.setdefault(pid, []).append((str(code), q or 0))
    codes = {str(r[0]) for r in c.execute('SELECT Code FROM StoreItems')}
    for pid, cat, code in c.execute('SELECT PID,Cat,Code FROM Products'):
        if pid not in um and cat == FISH_CAT and code and str(code) in codes:
            um[pid] = [(str(code), 1.0)]
    return um


# ★★★ [EDIT-MAP] حساب تكلفة الصنف من الريسبي ★★★
def unit_cost(c, pid, um, pr):
    if pid in um:
        return round(sum(q * pr.get(code, 0) for code, q in um[pid]), 4)
    r = c.execute('SELECT Cost FROM Products WHERE PID=?', (pid,)).fetchone()
    return (r[0] or 0) if r else 0


# ★★★ [EDIT-MAP] حركة الخامات ★★★
def mat_move(c, lo, hi, closed_only=False, kinds=None):
    """حركة الخامات في الفترة [lo,hi): وارد (طلبيات تم الاستلام) / مستهلك (مبيعات × الريسبي) / تسويات الجرد"""
    recv, cons, adj = {}, {}, {}
    for code, q in c.execute("SELECT i.Code,SUM(IFNULL(i.RQty,0)) FROM POItems i JOIN POrders o ON o.POID=i.POID "
                             "WHERE o.Stage='received' AND substr(o.RecvAt,1,10)>=? AND substr(o.RecvAt,1,10)<? GROUP BY i.Code", (lo, hi)):
        recv[str(code)] = q or 0
    um = usage_map(c)
    sql = ('SELECT i.PID,SUM(i.Qty) FROM SaleItems i JOIN Sales s ON s.SID=i.SID WHERE IFNULL(s.Void,0)=0 AND s.SDay>=? AND s.SDay<?'
           + (' AND IFNULL(s.Closed,0)=1' if closed_only else '') + ' GROUP BY i.PID')
    for pid, q in c.execute(sql, (lo, hi)):
        for code, per in um.get(pid, []):
            cons[code] = cons.get(code, 0) + (q or 0) * per
    sql = 'SELECT Code,SUM(Qty) FROM StoreAdj WHERE Day>=? AND Day<?' + (' AND Kind IN (%s)' % ','.join('?' * len(kinds)) if kinds else '') + ' GROUP BY Code'
    for code, q in c.execute(sql, (lo, hi) + tuple(kinds or ())):
        adj[str(code)] = q or 0
    return recv, cons, adj


def _month(m):
    m = str(m or '')[:7]
    if not re.match(r'^\d{4}-(0[1-9]|1[0-2])$', m):
        raise Exception('شهر غير صحيح')
    return m


def _prev_month(m):
    y, mo = int(m[:4]), int(m[5:7])
    return '%04d-%02d' % ((y - 1, 12) if mo == 1 else (y, mo - 1))


def _st_calc(c, m):
    lo, hi = m + '-01', m + '-32'
    pr, pc, pa = mat_move(c, '0000-00-00', lo)
    rc, cs, _ = mat_move(c, lo, hi)
    open_adj = {str(r[0]): r[1] for r in c.execute("SELECT Code,Qty FROM StoreAdj WHERE Month=? AND Kind='open'", (m,))}
    prev = c.execute("SELECT 1 FROM StocktakeMeta WHERE Month=? AND Status='closed'", (_prev_month(m),)).fetchone()
    out = {}
    for (code,) in c.execute('SELECT Code FROM StoreItems').fetchall():
        k = str(code)
        prior = pr.get(k, 0) - pc.get(k, 0) + pa.get(k, 0)
        opening = prior + (0 if prev else open_adj.get(k, 0))
        out[k] = dict(prior=prior, opening=round(opening, 3), received=round(rc.get(k, 0), 3), used=round(cs.get(k, 0), 3),
                      expected=round(opening + rc.get(k, 0) - cs.get(k, 0), 3))
    return out, bool(prev)


# ★★★ [EDIT-MAP] جرد الشهر ★★★
def stocktake_get(month):
    m = _month(month)
    with db() as c:
        meta = c.execute('SELECT Status,ClosedBy,ClosedAt FROM StocktakeMeta WHERE Month=?', (m,)).fetchone()
        calc, fixed = _st_calc(c, m)
        pr = last_prices(c)
        mine = {str(r[0]): r[1] for r in c.execute('SELECT Code,Actual FROM StockCount WHERE Month=?', (m,))}
        rows = []
        for code, name, unit, cat in c.execute('SELECT Code,IName,Unit,Cat FROM StoreItems ORDER BY Cat,Code').fetchall():
            k = str(code)
            x = calc[k]
            rows.append(dict(code=k, name=name, unit=unit or 'kg', cat=cat or '', opening=x['opening'], fixed=fixed,
                             received=x['received'], used=x['used'], expected=x['expected'], actual=mine.get(k),
                             price=pr.get(k, 0), value=round(x['expected'] * pr.get(k, 0), 2)))
        return dict(month=m, status=(meta[0] if meta else 'open'), closedBy=(meta[1] if meta else '') or '',
                    closedAt=(meta[2] if meta else '') or '', rows=rows)


def stocktake_save(d):
    m = _month(d.get('month'))
    with LOCK, db() as c:
        st = c.execute('SELECT Status FROM StocktakeMeta WHERE Month=?', (m,)).fetchone()
        if st and st[0] == 'closed':
            raise Exception('جرد الشهر ده اتعتمد - مينفعش يتعدّل')
        calc, fixed = _st_calc(c, m)
        for r in d.get('rows') or []:
            k = str(r['code'])
            if k not in calc:
                continue
            old = c.execute('SELECT Opening,Actual FROM StockCount WHERE Month=? AND Code=?', (m, k)).fetchone()
            op, ac = (old[0], old[1]) if old else (None, None)
            if 'opening' in r and not fixed:
                if r['opening'] in (None, ''):
                    op = None
                    c.execute("DELETE FROM StoreAdj WHERE Month=? AND Code=? AND Kind='open'", (m, k))
                else:
                    op = round(float(r['opening']), 3)
                    c.execute("INSERT OR REPLACE INTO StoreAdj (Month,Code,Kind,Day,Qty) VALUES (?,?,'open',?,?)",
                              (m, k, m + '-01', round(op - calc[k]['prior'], 3)))
            if 'actual' in r:
                ac = None if r['actual'] in (None, '') else round(float(r['actual']), 3)
                if ac is not None and ac < 0:
                    raise Exception('كمية غير صحيحة')
            c.execute('INSERT OR REPLACE INTO StockCount (Month,Code,Opening,Actual) VALUES (?,?,?,?)', (m, k, op, ac))


def stocktake_close(d, user):
    """اعتماد الجرد: الفرق بين العدد الفعلي والمتوقع بيتسجل تسوية، فرصيد المخزن بيبقى = العدد الفعلي ورصيد أول الشهر الجاي كمان"""
    m = _month(d.get('month'))
    with LOCK, db() as c:
        if c.execute("SELECT 1 FROM StocktakeMeta WHERE Month=? AND Status='closed'", (m,)).fetchone():
            return
        calc, _ = _st_calc(c, m)
        for k, ac in c.execute('SELECT Code,Actual FROM StockCount WHERE Month=? AND Actual IS NOT NULL', (m,)).fetchall():
            if str(k) in calc:
                c.execute("INSERT OR REPLACE INTO StoreAdj (Month,Code,Kind,Day,Qty) VALUES (?,?,'count',?,?)",
                          (m, str(k), m + '-31', round(ac - calc[str(k)]['expected'], 3)))
        c.execute('INSERT OR REPLACE INTO StocktakeMeta (Month,Status,ClosedBy,ClosedAt) VALUES (?,?,?,?)',
                  (m, 'closed', full_name(c, user['u']), now_s()))


def stocktake_reopen(d):
    m = _month(d.get('month'))
    with LOCK, db() as c:
        if c.execute("SELECT 1 FROM StocktakeMeta WHERE Month>? AND Status='closed'", (m,)).fetchone():
            raise Exception('فيه جرد شهر بعده معتمد - افتح الأحدث الأول')
        c.execute("DELETE FROM StoreAdj WHERE Month=? AND Kind='count'", (m,))
        c.execute('DELETE FROM StocktakeMeta WHERE Month=?', (m,))


# ★★★ [EDIT-MAP] حفظ الريسبي ★★★
def recipe_save(d):
    pid = int(d['pid'])
    with LOCK, db() as c:
        codes = {str(r[0]) for r in c.execute('SELECT Code FROM StoreItems')}
        rows = {}
        for r in d.get('rows') or []:
            k, q = str(r['code']).strip(), round(float(r['qty']), 4)
            if k not in codes:
                raise Exception('الكود %s مش موجود في الموردين والأكواد' % k)
            if q <= 0:
                raise Exception('كمية غير صحيحة للكود %s' % k)
            rows[k] = round(rows.get(k, 0) + q, 4)
        c.execute('DELETE FROM Recipe WHERE PID=?', (pid,))
        c.executemany('INSERT INTO Recipe (PID,Code,Qty) VALUES (?,?,?)', [(pid, k, q) for k, q in rows.items()])
        if 'cost' in d:
            c.execute('UPDATE Products SET Cost=? WHERE PID=?', (float(d.get('cost') or 0), pid))


# ★★★ [EDIT-MAP] تقارير المخزن والأرباح ★★★
def report_stock(frm, to):
    """تقرير المخزن + تكلفة وربح الأصناف في فترة"""
    lo, hi = (frm or '0000-00-00'), ((to or '9999-99-99') + ' ')
    with db() as c:
        sync_fish(c)
        pr = last_prices(c)
        b_rc, b_cs, b_ad = mat_move(c, '0000-00-00', lo)
        rc, cs, ad = mat_move(c, lo, hi)
        buy = {str(r[0]): (r[1] or 0) for r in c.execute(
            "SELECT i.Code,SUM(IFNULL(i.RQty,0)*IFNULL(i.Price,0)) FROM POItems i JOIN POrders o ON o.POID=i.POID "
            "WHERE o.Stage='received' AND substr(o.RecvAt,1,10)>=? AND substr(o.RecvAt,1,10)<? GROUP BY i.Code", (lo, hi))}
        mats, sv = [], 0.0
        for code, name, unit in c.execute('SELECT Code,IName,Unit FROM StoreItems ORDER BY Cat,Code').fetchall():
            k = str(code)
            op = b_rc.get(k, 0) - b_cs.get(k, 0) + b_ad.get(k, 0)
            cl = op + rc.get(k, 0) - cs.get(k, 0) + ad.get(k, 0)
            val = cl * pr.get(k, 0)
            sv += val
            mats.append(dict(code=k, name=name, unit=unit, opening=round(op, 3), received=round(rc.get(k, 0), 3), used=round(cs.get(k, 0), 3),
                             adj=round(ad.get(k, 0), 3), closing=round(cl, 3), price=pr.get(k, 0), value=round(val, 2), bought=round(buy.get(k, 0), 2)))
        um = usage_map(c)
        prods, tr, tc = [], 0.0, 0.0
        for pid, name, cat, unit, q, rev in c.execute(
                'SELECT i.PID,i.IName,i.Cat,i.Unit,SUM(i.Qty),SUM(i.Qty*i.Price) FROM SaleItems i JOIN Sales s ON s.SID=i.SID '
                'WHERE IFNULL(s.Void,0)=0 AND s.SDay>=? AND s.SDay<? GROUP BY i.PID,i.IName ORDER BY 6 DESC', (lo, hi)).fetchall():
            uc = unit_cost(c, pid, um, pr)
            cost = (q or 0) * uc
            tr += rev or 0
            tc += cost
            prods.append(dict(id=pid, name=name, cat=cat or '', unit=unit, qty=round(q or 0, 3), revenue=round(rev or 0, 2), ucost=round(uc, 3),
                              cost=round(cost, 2), profit=round((rev or 0) - cost, 2), nocost=(uc == 0)))
        return dict(mats=mats, prods=prods, totals=dict(revenue=round(tr, 2), cost=round(tc, 2), profit=round(tr - tc, 2),
                                                        stockValue=round(sv, 2), bought=round(sum(buy.values()), 2)))


def do_po_stage(d, user):
    """تغيير حالة الطلبية: progress (جاري التنفيذ) / received (تم الاستلام: بيزوّد المخزن مرة واحدة) / cancelled (ملغاة)"""
    stage = d.get('stage')
    if stage not in ('progress', 'received', 'cancelled'):
        raise Exception('حالة غير صحيحة')
    with LOCK, db() as c:
        r = c.execute('SELECT Stage FROM POrders WHERE POID=?', (d['id'],)).fetchone()
        if not r:
            raise Exception('الطلبية غير موجودة')
        cur = r[0] or 'progress'
        if cur == stage:
            return
        if cur == 'received':
            raise Exception('الطلبية اتسجلت \"تم الاستلام\" واتزوّد المخزن - مينفعش يتغيّر')
        if stage == 'received':
            if cur == 'cancelled':
                raise Exception('الطلبية ملغاة - أعد فتحها الأول')
            got = {str(k): float(v) for k, v in (d.get('recv') or {}).items()}
            for code, name, qty in c.execute('SELECT Code,IName,Qty FROM POItems WHERE POID=?', (d['id'],)).fetchall():
                q = round(got.get(str(code), qty), 3)
                if q < 0:
                    raise Exception('كمية غير صحيحة للكود %s' % code)
                c.execute('UPDATE POItems SET RQty=? WHERE POID=? AND Code=?', (q, d['id'], code))
            c.execute('UPDATE POrders SET Stage=?,RecvAt=?,RecvBy=? WHERE POID=?', (stage, now_s(), full_name(c, user['u']), d['id']))
        else:
            c.execute('UPDATE POrders SET Stage=?,RecvAt=?,RecvBy=? WHERE POID=?', (stage, '', '', d['id']))


def po_mark(poid, status, err=''):
    with LOCK, db() as c:
        c.execute('UPDATE POrders SET Status=?,Err=?,SentAt=? WHERE POID=?',
                  (status, err[:200], now_s() if status == 'sent' else '', poid))


def do_po_send(d):
    """إرسال الطلبية بـ HTTP للعنوان اللي في الإعدادات (poIp/poPort/poPath). لو فشل: بتفضل 'في الانتظار' وتتبعت بعدين"""
    import urllib.request
    with db() as c:
        cfg = get_cfg(c)
        r = c.execute('SELECT %s FROM POrders WHERE POID=?' % PO_COLS, (d['id'],)).fetchone()
        o = po_dict(c, r) if r else None
    if not o:
        raise Exception('الطلبية غير موجودة')
    ip = str(cfg.get('poIp') or '').strip()
    if cfg.get('poMode') != 'http' or not ip:
        raise Exception('إرسال الطلبيات HTTP غير مفعّل في الإعدادات')
    path = str(cfg.get('poPath') or '/')
    url = ip if ip.startswith('http') else 'http://%s:%d%s' % (ip, int(cfg.get('poPort') or 80), path if path.startswith('/') else '/' + path)
    body = json.dumps({'type': 'purchase_order', 'restaurant': cfg.get('name', ''), 'code': o['no'], 'date': o['day'],
                       'time': o['time'], 'supplier': o['supplier'], 'total': o['total'], 'note': o['note'],
                       'items': [dict(i, total=round(i['qty'] * i['price'], 2)) for i in o['items']]}, ensure_ascii=False).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json; charset=utf-8'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            if not 200 <= resp.status < 300:
                raise Exception('HTTP %s' % resp.status)
        po_mark(d['id'], 'sent')
        return {'ok': 1, 'status': 'sent'}
    except Exception as e:
        po_mark(d['id'], 'pending', str(e))
        return {'ok': 1, 'status': 'pending', 'err': str(e)[:200]}


def save_supplier(d):
    with LOCK, db() as c:
        if d.get('del'):
            c.execute('DELETE FROM Suppliers WHERE SupID=?', (int(d['id']),))
        elif d.get('id'):
            c.execute('UPDATE Suppliers SET SName=?,Phone=?,Note=? WHERE SupID=?', (d['name'], d.get('phone', ''), d.get('note', ''), int(d['id'])))
        else:
            if not str(d.get('name', '')).strip():
                raise Exception('اكتب اسم المورد')
            c.execute('INSERT INTO Suppliers (SName,Phone,Note) VALUES (?,?,?)', (d['name'].strip(), d.get('phone', ''), d.get('note', '')))


def save_store_item(d):
    code, old = str(d.get('code', '')).strip(), str(d.get('old_code') or d.get('code', '')).strip()
    with LOCK, db() as c:
        if d.get('del'):
            c.execute('DELETE FROM StoreItems WHERE Code=?', (old,))
            return
        if not code or not str(d.get('name', '')).strip():
            raise Exception('الكود والاسم مطلوبين')
        if old != code and c.execute('SELECT 1 FROM StoreItems WHERE Code=?', (code,)).fetchone():
            raise Exception('الكود مستخدم لصنف تاني')
        vals = (code, d['name'].strip(), d.get('unit', 'kg'), d.get('cat', ''), float(d.get('price') or 0), int(d.get('supid') or 0))
        if c.execute('SELECT 1 FROM StoreItems WHERE Code=?', (old,)).fetchone():
            c.execute('UPDATE StoreItems SET Code=?,IName=?,Unit=?,Cat=?,Price=?,SupID=? WHERE Code=?', vals + (old,))
        else:
            c.execute('INSERT INTO StoreItems (Code,IName,Unit,Cat,Price,SupID) VALUES (?,?,?,?,?,?)', vals)


def save_template(d):
    with LOCK, db() as c:
        if d.get('del'):
            c.execute('DELETE FROM PoTemplates WHERE TID=?', (int(d['id']),))
            return
        name = str(d.get('name', '')).strip()
        if not name or not d.get('items'):
            raise Exception('اكتب اسم القالب وضيف أصناف')
        items = json.dumps([{'code': str(i['code']), 'qty': float(i['qty'])} for i in d['items']], ensure_ascii=False)
        if d.get('id'):
            c.execute('UPDATE PoTemplates SET TName=?,Items=? WHERE TID=?', (name, items, int(d['id'])))
        else:
            c.execute('INSERT INTO PoTemplates (TName,Items) VALUES (?,?)', (name, items))


# ------------------------------------------------------------------ backups (v3) - يدوي بس، من غير مسح أوتوماتيكي
BK_DIR = os.path.join(os.path.dirname(DB), 'backups')
BK_RE = re.compile(r'^pos_backup_(\d{8})_(\d{6})\.db$')


# ★★★ [EDIT-MAP] النسخ الاحتياطي ★★★
def make_backup():
    os.makedirs(BK_DIR, exist_ok=True)
    name = 'pos_backup_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.db'
    dst = os.path.join(BK_DIR, name)
    src, out = sqlite3.connect(DB, timeout=30), sqlite3.connect(dst)
    try:
        src.backup(out)  # نسخة متسقة حتى لو في بيع شغال
    finally:
        out.close()
        src.close()
    return {'name': name, 'size': os.path.getsize(dst)}


def bk_time(name):
    m = BK_RE.match(name)
    return datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S') if m else None


def list_backups():
    if not os.path.isdir(BK_DIR):
        return []
    out = []
    for n in os.listdir(BK_DIR):
        t = bk_time(n)
        if t:
            out.append({'name': n, 'size': os.path.getsize(os.path.join(BK_DIR, n)), 'at': t.strftime('%Y-%m-%d %H:%M:%S')})
    return sorted(out, key=lambda x: x['name'], reverse=True)


def clean_backups(days):
    # بيمسح بس النسخ الأقدم من المدة اللي اتحددت (قرار يدوي)
    days = int(days)
    if days < 1:
        raise Exception('مدة غير صحيحة')
    cut, n = datetime.now() - timedelta(days=days), 0
    for f in list_backups():
        if bk_time(f['name']) < cut:
            os.remove(os.path.join(BK_DIR, f['name']))
            n += 1
    return n


# ------------------------------------------------------------------ الفرع / الشبكة (v5)
def local_ips():
    # بيجيب عناوين IP بتاعة الجهاز على الشبكة عشان تتحط في إعدادات الفرع وتتفتح من الأجهزة التانية
    ips = set()
    try:
        for r in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(r[4][0])
    except OSError:
        pass
    try:
        so = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        so.connect(('10.255.255.255', 1))
        ips.add(so.getsockname()[0])
        so.close()
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith('127.') and not i.startswith('169.254.'))


def site_info():
    with db() as c:
        cfg = get_cfg(c)
    return {'name': cfg.get('name', ''), 'branchName': cfg.get('branchName', ''), 'branchCode': cfg.get('branchCode', ''), 'ver': VERSION}


# ------------------------------------------------------------------ http
# ★★★ [EDIT-MAP] 📡 كل مسارات الـ API (do_GET / do_POST) ★★★
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def out(self, code, obj, ct='application/json'):
        b = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ct + '; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def user(self):
        return TOK.get(self.headers.get('X-Token', ''))

    def events(self):
        # بث لحظي (Server-Sent Events): كل جهاز بيسمع لأي أوردر/تعديل بيحصل من جهاز تاني
        q = parse_qs(urlparse(self.path).query)
        if not TOK.get((q.get('t') or [''])[0]):
            return self.out(401, {'error': 'login'})
        sub = queue.Queue(maxsize=500)
        with SUBS_LOCK:
            SUBS.add(sub)
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            self.wfile.write(b'retry: 2000\n\n')
            self.wfile.flush()
            while True:
                try:
                    ev = sub.get(timeout=15)
                    self.wfile.write(('data: ' + json.dumps(ev, ensure_ascii=False) + '\n\n').encode('utf-8'))
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with SUBS_LOCK:
                SUBS.discard(sub)

    def download(self):
        q = parse_qs(urlparse(self.path).query)
        u = TOK.get((q.get('t') or [''])[0])
        name = (q.get('name') or [''])[0]
        if not u or u['role'] != 'admin' or not BK_RE.match(name) or not os.path.isfile(os.path.join(BK_DIR, name)):
            return self.out(403, {'error': 'forbidden'})
        b = open(os.path.join(BK_DIR, name), 'rb').read()
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Disposition', 'attachment; filename="%s"' % name)
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ('/', '/index.html'):
            try:
                return self.out(200, open(HTML, 'rb').read(), 'text/html')
            except OSError:
                return self.out(500, {'error': 'pos_istakoza.html not found'})
        if p == '/api/site':
            return self.out(200, site_info())
        if p == '/api/events':
            return self.events()
        if p == '/api/backup/download':
            return self.download()
        u = self.user()
        if not u:
            return self.out(401, {'error': 'login'})
        try:
            if p == '/api/state':
                if u['role'] == 'driver': return self.out(200, {'products':[],'sales':[],'archive':[],'stock':{},'cfg':{},'ver':VERSION,'me':u})
                return self.out(200, dict(get_state(), me=u))
            if p == '/api/users' and u['role'] == 'admin':
                return self.out(200, list_users())
            if p == '/api/delivery' and u['role'] in ('admin','cashier','driver'):
                return self.out(200, delivery_get(u))
            if p == '/api/netinfo' and u['role'] == 'admin':
                return self.out(200, {'ips': local_ips(), 'port': PORT, 'host': socket.gethostname(), 'db': DB, 'html': HTML, 'ver': VERSION})
            if p == '/api/stocktake' and can(u, 'stock'):
                q = parse_qs(urlparse(self.path).query)
                return self.out(200, stocktake_get((q.get('month') or [''])[0]))
            if p == '/api/report/stock' and can(u, 'rep'):
                q = parse_qs(urlparse(self.path).query)
                return self.out(200, report_stock((q.get('from') or [''])[0], (q.get('to') or [''])[0]))
            if p == '/api/store' and can(u, 'po'):
                return self.out(200, store_extra(get_store()))
            if p == '/api/backups' and u['role'] == 'admin':
                return self.out(200, {'dir': BK_DIR, 'files': list_backups()})
            if p == '/api/log':
                return self.out(200, get_log((parse_qs(urlparse(self.path).query).get('sid') or [''])[0]))
            self.out(403, {'error': 'forbidden'})
        except Exception as e:
            self.out(500, {'error': str(e)})

    def do_POST(self):
        try:
            d = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        except Exception:
            d = {}
        p = self.path
        try:
            if p == '/api/login':
                u = login(d.get('u', ''), d.get('p', ''))
                if not u:
                    return self.out(400, {'error': 'اسم المستخدم أو كلمة السر غير صحيحة'})
                t = secrets.token_hex(16)
                TOK[t] = u
                return self.out(200, dict(u, token=t))
            u = self.user()
            if not u:
                return self.out(401, {'error': 'login'})
            adm = u['role'] == 'admin'
            cid = self.headers.get('X-Cid', '')
            if p == '/api/logout':
                TOK.pop(self.headers.get('X-Token'), None)
            elif p == '/api/state':
                save_state(d, u['role'] if adm or can(u, 'price') else 'none')
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/sale':
                r = add_sale(d, u)
                if not r.get('dup'):
                    broadcast({'t': 'sale', 'sale': d, 'who': u['full'] or u['u'], 'cid': cid})
                return self.out(200, r)
            elif p == '/api/delivery/open-shift' and (adm or u['role']=='driver'):
                if u['role']=='driver': d['did']=next((x['DID'] for x in db_driver_list() if x['UName']==u['u']),0)
                delivery_open_shift(d,u); broadcast({'t':'reload','cid':cid})
            elif p == '/api/delivery/load' and (adm or u['role']=='cashier'):
                delivery_assign(d,u); broadcast({'t':'reload','cid':cid})
            elif p == '/api/delivery/withdraw' and (adm or u['role']=='cashier'):
                delivery_withdraw(d,u); broadcast({'t':'reload','cid':cid})
            elif p == '/api/delivery/return-one' and (adm or u['role']=='cashier'):
                delivery_return_one(d,u); broadcast({'t':'reload','cid':cid})
            elif p == '/api/delivery/return-all' and (adm or u['role']=='cashier'):
                delivery_return_all(d,u); broadcast({'t':'reload','cid':cid})
            elif p == '/api/delivery/close-shift' and (adm or u['role']=='driver'):
                if u['role']=='driver': d['did']=next((x['DID'] for x in db_driver_list() if x['UName']==u['u']),0)
                r=delivery_close_shift(d,u); broadcast({'t':'reload','cid':cid}); return self.out(200,dict({'ok':1},**r))
            elif p == '/api/delivery/collect' and (adm or u['role'] in ('cashier','driver')):
                delivery_collect(d,u); broadcast({'t':'reload','cid':cid})
            elif p == '/api/close' and can(u, 'close'):
                with LOCK, db() as c:
                    for i in d['ids']:
                        r = c.execute('SELECT SDay FROM Sales WHERE SID=?', (i,)).fetchone()
                        c.execute('UPDATE Sales SET Closed=1 WHERE SID=?', (i,))
                        if r:  # تسجيل اليوم في day_locks
                            c.execute('INSERT OR IGNORE INTO day_locks (day,locked_at,locked_by) VALUES (?,?,?)', (r[0], now_s(), u['u']))
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/void' and can(u, 'void'):
                do_void(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/edit' and can(u, 'edit'):
                do_edit(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/return' and can(u, 'return'):
                r = do_return(d, u)
                broadcast({'t': 'reload', 'cid': cid})
                return self.out(200, {'ok': 1, 'ret': r})
            elif adm and p == '/api/return/pay':
                do_return_pay(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/stocktake/save' and can(u, 'stock'):
                stocktake_save(d)
            elif p == '/api/stocktake/close' and can(u, 'stock'):
                stocktake_close(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif adm and p == '/api/recipe':
                recipe_save(d)
                broadcast({'t': 'reload', 'cid': cid})
            elif adm and p == '/api/stocktake/reopen':
                stocktake_reopen(d)
            elif p == '/api/po' and can(u, 'po'):
                return self.out(200, {'ok': 1, 'order': do_po(d, u)})
            elif p == '/api/po/send' and can(u, 'po'):
                return self.out(200, do_po_send(d))
            elif p == '/api/po/stage' and can(u, 'po'):
                do_po_stage(d, u)
                broadcast({'t': 'reload', 'cid': cid})
            elif p == '/api/po/mark' and can(u, 'po'):
                po_mark(d['id'], 'sent' if d.get('status') == 'sent' else 'pending', str(d.get('err', '')))
            elif adm and p == '/api/po/del':
                with LOCK, db() as c:
                    c.execute('DELETE FROM POItems WHERE POID=?', (d['id'],))
                    c.execute('DELETE FROM POrders WHERE POID=?', (d['id'],))
            elif adm and p == '/api/store/supplier':
                save_supplier(d)
            elif adm and p == '/api/store/item':
                save_store_item(d)
            elif p == '/api/store/template' and can(u, 'po'):
                save_template(d)
            elif p == '/api/print':
                do_print(d)
            elif adm and p == '/api/users':
                save_user(d)
            elif adm and p == '/api/backup':
                return self.out(200, dict(make_backup(), ok=1))
            elif adm and p == '/api/backup/clean':
                return self.out(200, {'ok': 1, 'deleted': clean_backups(d.get('days', 30))})
            elif adm and p == '/api/reset':
                reset()
                broadcast({'t': 'reload', 'cid': cid})
            elif adm and p == '/api/import':
                do_import(d)
                broadcast({'t': 'reload', 'cid': cid})
            else:
                return self.out(403, {'error': 'forbidden'})
            self.out(200, {'ok': 1})
        except Forbidden as e:
            self.out(403, {'error': str(e)})
        except Exception as e:
            print('POST ERROR:', repr(e))
            self.out(400, {'error': str(e)})


def open_pos(url):
    # بيفتح البرنامج كنافذة تطبيق بطباعة صامتة (--kiosk-printing) لو Chrome/Edge موجود، وإلا المتصفح العادي
    if os.name == 'nt':
        pf = [os.environ.get(k, '') for k in ('ProgramFiles', 'ProgramFiles(x86)', 'LocalAppData')]
        cands = [os.path.join(pf[0], 'Google', 'Chrome', 'Application', 'chrome.exe'),
                 os.path.join(pf[1], 'Google', 'Chrome', 'Application', 'chrome.exe'),
                 os.path.join(pf[2], 'Google', 'Chrome', 'Application', 'chrome.exe'),
                 os.path.join(pf[1], 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
                 os.path.join(pf[0], 'Microsoft', 'Edge', 'Application', 'msedge.exe')]
        prof = os.path.join(os.environ.get('LOCALAPPDATA', APP_DIR), 'IstakozaPOS_Browser')
        for b in cands:
            if os.path.isfile(b):
                try:
                    subprocess.Popen([b, '--kiosk-printing', '--user-data-dir=' + prof, '--app=' + url])
                    return
                except OSError:
                    pass
    webbrowser.open(url)


class Srv(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def port_busy():
    try:
        with socket.create_connection(('127.0.0.1', PORT), timeout=1):
            return True
    except OSError:
        return False


if __name__ == '__main__':
    if os.name == 'nt':
        os.system('chcp 65001 >nul')
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    url = 'http://localhost:%d' % PORT
    if port_busy():
        print('البرنامج شغال بالفعل (البورت %d مستخدم) - هفتحه في المتصفح' % PORT)
        if '--no-browser' not in sys.argv:
            open_pos(url)
        sys.exit(0)
    init()
    print('السيرفر شغال: %s   (من جهاز تاني: http://IP-الجهاز:%d)' % (url, PORT))
    print('مكان البيانات:', DB)
    print('الصفحة:', HTML)
    print('الدخول الأول: admin / admin  - غيّر كلمة السر من الإعدادات')
    print('متقفلش الشاشة السودا دي طول ما بتشتغل.')
    if '--no-browser' not in sys.argv:
        threading.Timer(1.2, lambda: open_pos(url)).start()
    Srv(('0.0.0.0', PORT), H).serve_forever()
