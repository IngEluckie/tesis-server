# auth.py

"""
Module developed to manage user authentication
"""

# Also include redis db (not now)

# Import libraries
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os
from pydantic import BaseModel
#import redis

# Importamos la clase para la base de datos
from database.singleton import Database

# Iniciamos router
router_authentication: APIRouter = APIRouter(prefix="/auth")
#r = redis.Redis(host='localhost', port=6379, db=0)

# Cargar variables de entorno
load_dotenv(override=True)

@router_authentication.get("/ison")
async def ison():
    return {"message": "Yeah! I'm on!"}

"""
Códigos y seguridad
"""

oauth2 = OAuth2PasswordBearer(tokenUrl="auth/login")

ALGORITHM = (os.getenv("ALGORITHM") or "HS256").strip('"').strip("'")

crypt =  CryptContext(schemes=["bcrypt"], deprecated="auto")

ACCESS_TOKEN_DURATION = int(os.getenv("ACCESS_TOKEN_DURATION", 30))

SECRET = (os.getenv("SECRET") or "").strip('"').strip("'")

if not SECRET:
    raise RuntimeError("SECRET env variable is missing. Set SECRET in the environment or .env file.")

"""
Model classes for users
"""

class User(BaseModel):
    user_id: int
    username: str
    name: str | None
    email: str | None

class UserPrivate(User):
    password: str
    typeUser: int | None

"""
Authentication process
"""

search_user_by_username_query = """
    SELECT * FROM Usuarios WHERE username = ?
"""

search_user_by_id_query = """
    SELECT * FROM Usuarios WHERE Id_Usuarios = ?
"""

def search_user_private(username: str):
    try:
        database: Database = Database()
        user_db_list: list = database.fetch_query(search_user_by_username_query, (username,))
        if not user_db_list:
            return None
        user_db = user_db_list[0]
        return UserPrivate(
            user_id=user_db["Id_Usuarios"],
            username=user_db["username"],
            name=user_db["NombreCompleto"],
            email=user_db["email"],
            password=user_db["Password"],
            typeUser=user_db["Tipo_usuario"],
        )

    except Exception as e:
        print(f"Error en search_user_private(): {e}")
        return None
    
def search_user(user_iD: int):
    try:
        database = Database()
        user_db_list = database.fetch_query(search_user_by_id_query, (user_iD,))
        if not user_db_list:
            return None
        user_db = user_db_list[0]
        return User(
            user_id=user_db["Id_Usuarios"],
            username=user_db["username"],
            name=user_db["NombreCompleto"],
            email=user_db["email"]
        )
    except Exception as e:
        print(f"Error en search_user: {e}")
        return None

invalid_token_exception: HTTPException = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid token",
    headers={"WWW-Authenticate": "Bearer"},
) 

async def auth_user(token: str = Depends(oauth2)):
    try:
        payload = jwt.decode(token, SECRET, algorithms=[ALGORITHM])
        print("Payload decodificado:", payload)  # Depuración
        user_iD_str = payload.get("sub")
        if user_iD_str is None:
            print("No se encontró 'sub' en el token")
            raise invalid_token_exception
        user_iD = int(user_iD_str)  # Convertir a entero
    except JWTError as e:
        print("Error al decodificar token:", e)
        raise invalid_token_exception

    user = search_user(user_iD)
    if user is None:
        print("Usuario no encontrado en la base de datos")
        raise invalid_token_exception
    return user

def current_user(user: User = Depends(auth_user)):
    return user

@router_authentication.post("/login")
async def login(form: OAuth2PasswordRequestForm = Depends()):
    # Se busca el usuario por username (que es de tipo string)
    user = search_user_private(form.username)
    if not user:
        print("Error: Usuario no encontrado")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect username or user does not exist"
        )

    #Verificamos la contraseña (asumiendo que está encriptada con bcrypt)
    if not crypt.verify(form.password, user.password):
        print("Error: Contraseña incorrecta")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect password"
        )

    #Generamos el token
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_DURATION)
    payload = {
        "sub": str(user.user_id),
        "exp": datetime.utcnow() + access_token_expires
    }
    encoded_jwt = jwt.encode(payload, SECRET, algorithm=ALGORITHM)

    return {
        "access_token": encoded_jwt,
        "token_type": "bearer",
        "dashboard": "/dashboard.html"
    }


"""
Get user's info
"""

@router_authentication.get("/me")
async def me(user: User = Depends(current_user)):
    return user

@router_authentication.get("/getUserInfo/{otherUser}")
async def getUserInfo(
    user: User = Depends(current_user),
    otherUser: str = ""
):
    try:
        db = Database()
        db_user = db.fetch_query(search_user_by_username_query, (otherUser,))
        if not db_user:
            return None
        userInfo = db_user[0]
        return User(
            user_id=userInfo["Id_Usuarios"],
            username=userInfo["username"],
            name=userInfo["NombreCompleto"],
            email=userInfo["email"],
        )
    except Exception as e:
        raise Exception

#router_authentication.get("/me")

#router_authentication.get("/getUserInfo/{otherUser}")
