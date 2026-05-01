import os
import uuid
import jwt
import httpx
import base64
import hashlib
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request, HTTPException, Header
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import aliased
from sqlalchemy import or_, and_, text
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from database import engine, Base, get_db
from models import User, Product, Order, PrivateMessage, BlockedUser, Review, Transaction, Ticket

SECRET_KEY = "tradeflow_super_secret"

VK_CLIENT_ID = "54566173" 
VK_CLIENT_SECRET = os.getenv("VK_CLIENT_SECRET")
VK_REDIRECT_URI = "https://wdai51.vercel.app/api/auth/vk/callback"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)
        # Автоматически добавляем новые колонки, если их нет
        try: await conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS images VARCHAR DEFAULT ''"))
        except: pass
        try: await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR DEFAULT 'user'"))
        except: pass
        try: await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_reason VARCHAR"))
        except: pass
        try: await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_until TIMESTAMP"))
        except: pass
    yield

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return JSONResponse(status_code=500, content={"detail": f"Внутренняя ошибка сервера: {str(exc)}"})

async def get_current_user(authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Не авторизован")
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        result = await db.execute(select(User).filter_by(id=int(payload.get("sub"))))
        user = result.scalar_one_or_none()
        if not user: raise HTTPException(status_code=401)
        
        # ПРОВЕРКА НА БАН
        if user.ban_until and user.ban_until > datetime.utcnow():
            raise HTTPException(status_code=403, detail={
                "banned": True, 
                "reason": user.ban_reason or "Нарушение правил", 
                "until": user.ban_until.strftime("%d.%m.%Y %H:%M")
            })
            
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Токен истек")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Недействительный токен")

class ImageUploadRequest(BaseModel):
    image_base64: str

@app.get("/api/auth/vk")
async def vk_login():
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8').rstrip('=')
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode('utf-8')).digest()).decode('utf-8').rstrip('=')
    url = f"https://id.vk.com/authorize?response_type=code&client_id={VK_CLIENT_ID}&redirect_uri={VK_REDIRECT_URI}&code_challenge={code_challenge}&code_challenge_method=S256&state=login"
    response = RedirectResponse(url)
    response.set_cookie(key="vk_code_verifier", value=code_verifier, httponly=True, max_age=600, secure=True, samesite="lax")
    return response

@app.get("/api/auth/vk/callback")
async def vk_callback(request: Request, code: str = None, device_id: str = None, state: str = None, error: str = None, error_description: str = None, db: AsyncSession = Depends(get_db)):
    try:
        if error: return RedirectResponse(url=f"/?error={error_description}")
        if not code: return RedirectResponse(url="/?error=ВК_не_прислал_код_подтверждения")
            
        code_verifier = request.cookies.get("vk_code_verifier")
        if not code_verifier: return RedirectResponse(url="/?error=Сессия_устарела_попробуйте_еще_раз")

        async with httpx.AsyncClient() as client:
            token_data_req = {
                "grant_type": "authorization_code", "client_id": VK_CLIENT_ID, "client_secret": VK_CLIENT_SECRET or "",
                "code": code, "code_verifier": code_verifier, "device_id": device_id or "",
                "redirect_uri": VK_REDIRECT_URI, "state": state or "login"
            }
            token_res = await client.post("https://id.vk.com/oauth2/auth", data=token_data_req, headers={"Content-Type": "application/x-www-form-urlencoded"})
            token_data = token_res.json()
            if "error" in token_data: return RedirectResponse(url=f"/?error=Ошибка_токена_{token_data.get('error_description', '')}")

            access_token = token_data.get("access_token")
            
            # Получаем расширенные данные профиля (включая domain/screen_name)
            old_res = await client.get(f"https://api.vk.com/method/users.get?fields=photo_100,domain,screen_name&access_token={access_token}&v=5.131")
            old_data = old_res.json()
            if "response" in old_data and len(old_data["response"]) > 0:
                u_info = old_data["response"][0]
                vk_user_id = int(u_info["id"])
                
                # Фикс "undefined"
                fn = u_info.get("first_name", "").replace("undefined", "").strip()
                ln = u_info.get("last_name", "").replace("undefined", "").strip()
                screen_name = u_info.get("screen_name", "") or u_info.get("domain", "")
                
                raw_name = f"{fn} {ln}".strip()
                username = raw_name if raw_name else f"User{vk_user_id}"
                avatar = u_info.get("photo_100", "")
            else:
                return RedirectResponse(url="/?error=Не_удалось_получить_профиль")

        res = await db.execute(select(User).filter_by(vk_id=vk_user_id))
        user = res.scalar_one_or_none()
        
        # Назначаем права админа для miellssd
        is_admin = (screen_name.lower() == "miellssd" or username.lower() == "miellssd")
        role = "admin" if is_admin else "user"

        if not user:
            user = User(vk_id=vk_user_id, username=username, avatar_url=avatar, balance=5000.0, role=role)
            db.add(user)
            await db.commit()
            tx = Transaction(user_id=user.id, type="topup", amount=5000.0, description="🎁 Приветственный бонус TradeFlow")
            db.add(tx)
            await db.commit()
            await db.refresh(user)
        else:
            # Обновляем роль, если он вдруг стал админом
            if is_admin and user.role != "admin":
                user.role = "admin"
                await db.commit()

        token = jwt.encode({"sub": str(user.id), "exp": datetime.utcnow() + timedelta(days=7)}, SECRET_KEY, algorithm="HS256")
        response = RedirectResponse(url=f"/?token={token}")
        response.delete_cookie("vk_code_verifier")
        return response
    except Exception as e:
        return RedirectResponse(url=f"/?error=Системная_ошибка_{str(e).replace(' ', '_')}")

@app.post("/api/upload_image")
async def upload_image(data: ImageUploadRequest):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(status_code=500, detail="Ключи Supabase не настроены в Vercel")
    try:
        base64_data = data.image_base64
        if "," in base64_data: base64_data = base64_data.split(",")[1]
        image_bytes = base64.b64decode(base64_data)
        
        filename = f"{uuid.uuid4().hex}.jpg"
        bucket_name = "tradeflow"
        clean_url = SUPABASE_URL.split('/rest/v1')[0].rstrip('/')
        upload_url = f"{clean_url}/storage/v1/object/{bucket_name}/{filename}"
        
        async with httpx.AsyncClient() as client:
            res = await client.post(upload_url, content=image_bytes, headers={"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY, "Content-Type": "image/jpeg"}, timeout=15.0)
            if res.status_code in (200, 201):
                return {"url": f"{clean_url}/storage/v1/object/public/{bucket_name}/{filename}"}
            else:
                raise HTTPException(status_code=400, detail=f"Ошибка Supabase: {res.text}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Сбой загрузки: {str(e)}")

@app.get("/api/user")
async def get_user(user: User = Depends(get_current_user)):
    return {"username": user.username, "balance": user.balance, "avatar_url": user.avatar_url, "id": user.id, "role": user.role}

# ИЗМЕНЕНИЕ ПРОФИЛЯ
@app.put("/api/users/me")
async def update_profile(data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if data.get("username"):
        # Проверяем занятость никнейма
        existing = await db.execute(select(User).filter(User.username == data["username"], User.id != user.id))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Этот никнейм уже занят")
        user.username = data["username"]
    
    if data.get("avatar_url"):
        user.avatar_url = data["avatar_url"]
        
    await db.commit()
    return {"status": "success"}

@app.get("/")
async def serve_frontend(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/api/products")
async def get_products(category: str = "All", subcategory: str = "Все", search: str = "", db: AsyncSession = Depends(get_db)):
    query = select(Product, User.username).join(User, Product.seller_id == User.id).filter(Product.status == "active")
    if category != "All": query = query.filter(Product.category == category)
    if subcategory and subcategory != "Все": query = query.filter(Product.subcategory == subcategory)
    if search: query = query.filter(Product.title.ilike(f"%{search}%"))
    result = await db.execute(query)
    return[{"id": p.id, "title": p.title, "price": p.price, "category": p.category, "subcategory": p.subcategory, "warranty": p.has_warranty, "seller": u, "images": p.images.split(',') if p.images else []} for p, u in result]

@app.post("/api/sell")
async def add_product(data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    images_str = ",".join([img for img in data.get('images', []) if img.strip()])[:2000]
    new_product = Product(
        seller_id=user.id, category=data['category'], subcategory=data.get('subcategory', 'Разное'),
        title=data['title'], description=data['description'], has_warranty=data['warranty'],
        price=float(data['price']), account_data=data['data'], images=images_str
    )
    db.add(new_product)
    await db.commit()
    return {"status": "success"}

@app.post("/api/buy/{product_id}")
async def buy_product(product_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    prod_res = await db.execute(select(Product).filter_by(id=product_id))
    product = prod_res.scalar_one_or_none()
    if not product or product.status != "active": raise HTTPException(400, "Товар недоступен")
    if product.seller_id == user.id: raise HTTPException(400, "Вы не можете купить свой собственный товар")
    if user.balance < product.price: raise HTTPException(400, "Недостаточно средств")
    
    user.balance -= product.price
    tx_buyer = Transaction(user_id=user.id, type="spend", amount=-product.price, description=f"🛒 Покупка: {product.title}")
    
    seller_res = await db.execute(select(User).filter_by(id=product.seller_id))
    seller = seller_res.scalar_one()
    seller.balance += product.price
    tx_seller = Transaction(user_id=seller.id, type="income", amount=product.price, description=f"💸 Продажа: {product.title}")
    
    product.status = "sold"
    order_code = "ORD-" + uuid.uuid4().hex[:8].upper()
    new_order = Order(order_code=order_code, buyer_id=user.id, seller_id=product.seller_id, product_id=product.id, price=product.price)
    
    db.add_all([tx_buyer, tx_seller, new_order])
    await db.commit()
    return {"status": "success", "order_code": order_code, "data": product.account_data}

@app.get("/api/finances")
async def get_finances(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    query = select(Transaction).filter_by(user_id=user.id).order_by(Transaction.timestamp.desc())
    result = await db.execute(query)
    txs = result.scalars().all()
    return[{"id": t.id, "type": t.type, "amount": t.amount, "description": t.description, "date": t.timestamp.strftime("%d.%m.%Y %H:%M")} for t in txs]

@app.get("/api/purchases")
async def get_purchases(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    query = select(Order, Product).join(Product, Order.product_id == Product.id).filter(Order.buyer_id == user.id)
    result = await db.execute(query)
    return[{"order_code": o.order_code, "title": p.title, "price": o.price, "data": p.account_data, "product_id": p.id, "seller_id": p.seller_id} for o, p in result]

@app.get("/api/users/{username}")
async def get_user_profile(username: str, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(User).filter_by(username=username))
    u = res.scalar_one_or_none()
    if not u: raise HTTPException(404, "Пользователь не найден")
    
    prod_res = await db.execute(select(Product).filter_by(seller_id=u.id, status="active"))
    products = prod_res.scalars().all()
    
    Buyer = aliased(User)
    rev_res = await db.execute(select(Review, Buyer.username).join(Buyer, Review.buyer_id == Buyer.id).filter(Review.seller_id == u.id))
    
    reviews =[]
    for r, buyer_name in rev_res:
        date_str = r.timestamp.strftime("%d.%m.%Y") if r.timestamp else "Недавно"
        reviews.append({"id": r.id, "buyer": buyer_name, "text": r.text, "reply": r.seller_reply, "date": date_str})
    
    return {
        "username": u.username, "avatar_url": u.avatar_url, "id": u.id, "role": u.role,
        "products":[{"id": p.id, "title": p.title, "price": p.price, "category": p.category, "images":[img for img in p.images.split(',') if img] if p.images else[]} for p in products],
        "reviews": reviews
    }

@app.post("/api/reviews")
async def post_review(data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rev_res = await db.execute(select(Review).filter_by(buyer_id=user.id, product_id=data['product_id']))
    review = rev_res.scalar_one_or_none()
    action = "оставил"
    if review:
        review.text = data['text']
        action = "изменил"
    else:
        review = Review(product_id=data['product_id'], buyer_id=user.id, seller_id=data['seller_id'], text=data['text'])
        db.add(review)
        
    sys_msg = PrivateMessage(sender_id=user.id, receiver_id=data['seller_id'], text=f"📢 Покупатель {action} отзыв: «{data['text']}»")
    db.add(sys_msg)
    await db.commit()
    return {"status": "success"}

@app.post("/api/reviews/{review_id}/reply")
async def reply_review(review_id: int, data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Review).filter_by(id=review_id, seller_id=user.id))
    review = res.scalar_one_or_none()
    if not review: raise HTTPException(403, "Отзыв не найден или вы не продавец")
    review.seller_reply = data['reply']
    await db.commit()
    return {"status": "success"}

@app.get("/api/chats")
async def get_chats(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    query = select(PrivateMessage).filter(or_(PrivateMessage.sender_id == user.id, PrivateMessage.receiver_id == user.id)).order_by(PrivateMessage.timestamp.desc())
    res = await db.execute(query)
    msgs = res.scalars().all()
    
    contacts_dict = {}
    for m in msgs:
        other_id = m.receiver_id if m.sender_id == user.id else m.sender_id
        if other_id not in contacts_dict:
            contacts_dict[other_id] = m.text
            
    chat_list =[]
    for uid, last_msg in contacts_dict.items():
        u_res = await db.execute(select(User).filter_by(id=uid))
        u = u_res.scalar_one_or_none()
        if u:
            chat_list.append({"username": u.username, "avatar_url": u.avatar_url, "last_message": last_msg})
    return chat_list

@app.get("/api/messages/{username}")
async def get_private_chat(username: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    target_res = await db.execute(select(User).filter_by(username=username))
    target = target_res.scalar_one_or_none()
    if not target: raise HTTPException(404)
    query = select(PrivateMessage).filter(or_(and_(PrivateMessage.sender_id == user.id, PrivateMessage.receiver_id == target.id), and_(PrivateMessage.sender_id == target.id, PrivateMessage.receiver_id == user.id))).order_by(PrivateMessage.timestamp)
    msgs = await db.execute(query)
    return[{"sender": user.username if m.sender_id == user.id else target.username, "text": m.text} for m in msgs.scalars().all()]

@app.post("/api/messages/{username}")
async def send_private_message(username: str, data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    target_res = await db.execute(select(User).filter_by(username=username))
    target = target_res.scalar_one_or_none()
    if not target: raise HTTPException(404, "Пользователь не найден")
    block_check = await db.execute(select(BlockedUser).filter_by(user_id=target.id, blocked_id=user.id))
    if block_check.scalar_one_or_none(): raise HTTPException(403, "Этот пользователь заблокировал вас")
    
    msg = PrivateMessage(sender_id=user.id, receiver_id=target.id, text=data['text'])
    db.add(msg)
    await db.commit()
    return {"status": "ok"}

@app.post("/api/users/{username}/block")
async def block_user(username: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    target_res = await db.execute(select(User).filter_by(username=username))
    target = target_res.scalar_one_or_none()
    if not target: raise HTTPException(404)
    block = BlockedUser(user_id=user.id, blocked_id=target.id)
    db.add(block)
    await db.commit()
    return {"status": "blocked"}

# ==========================================
# ТИКЕТЫ ПОДДЕРЖКИ
# ==========================================
@app.post("/api/tickets")
async def create_ticket(data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    ticket = Ticket(user_id=user.id, category=data['category'], text=data['text'])
    db.add(ticket)
    await db.commit()
    return {"status": "success"}

@app.get("/api/tickets")
async def get_my_tickets(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Ticket).filter_by(user_id=user.id).order_by(Ticket.timestamp.desc()))
    return[{"id": t.id, "category": t.category, "text": t.text, "status": t.status, "reply": t.admin_reply, "date": t.timestamp.strftime("%d.%m.%Y")} for t in res.scalars().all()]

# ==========================================
# АДМИН-ПАНЕЛЬ
# ==========================================
def require_admin(user: User = Depends(get_current_user)):
    if user.role != "admin": raise HTTPException(status_code=403, detail="Доступ запрещен")
    return user

@app.delete("/api/admin/products/{product_id}")
async def admin_delete_product(product_id: int, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    prod_res = await db.execute(select(Product).filter_by(id=product_id))
    product = prod_res.scalar_one_or_none()
    if product:
        await db.delete(product)
        await db.commit()
    return {"status": "deleted"}

@app.post("/api/admin/ban")
async def admin_ban_user(data: dict, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    target_res = await db.execute(select(User).filter_by(username=data['username']))
    target = target_res.scalar_one_or_none()
    if not target: raise HTTPException(404, "Пользователь не найден")
    
    days = int(data.get('days', 36500)) # По умолчанию навсегда (100 лет)
    target.ban_until = datetime.utcnow() + timedelta(days=days)
    target.ban_reason = data.get('reason', 'Нарушение правил маркетплейса')
    await db.commit()
    return {"status": "banned"}

@app.get("/api/admin/tickets")
async def admin_get_tickets(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Ticket, User.username).join(User, Ticket.user_id == User.id).order_by(Ticket.timestamp.desc()))
    return[{"id": t.id, "username": u, "category": t.category, "text": t.text, "status": t.status, "reply": t.admin_reply, "date": t.timestamp.strftime("%d.%m.%Y")} for t, u in res]

@app.post("/api/admin/tickets/{ticket_id}/reply")
async def admin_reply_ticket(ticket_id: int, data: dict, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Ticket).filter_by(id=ticket_id))
    ticket = res.scalar_one_or_none()
    if ticket:
        ticket.admin_reply = data['reply']
        ticket.status = "closed"
        await db.commit()
    return {"status": "success"}