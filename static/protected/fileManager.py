# fileManager.py

"""
Bodyguard for all protected files
"""

# Import libreries
from dataclasses import dataclass
from pathlib import Path
import mimetypes
import shutil
import io
import re
from typing import Iterable
from uuid import uuid4
from contextlib import contextmanager

from PIL import Image, ImageOps

# Import modules

# Global variables and functions
@dataclass
class ProfileImageInfo:
    user_id: int
    filename: str
    relative_path: str
    absolute_path: str

    @property
    def path(self) -> str:
        return self.absolute_path


@dataclass
class AttachmentInfo:
    chat_id: int
    sender_id: int
    original_filename: str
    stored_filename: str
    relative_path: str
    absolute_path: str
    mime_type: str
    size_bytes: int


@dataclass
class Collection:
    # Cada folder será una colección
    # Ejemplo: ImagenesPerfil
    name: str

def getFolders(root: str | Path | None = None) -> list[str]:
    # Retorna los nombres de los folders que se encuentran en la carpeta indicada
    base_path = Path(root) if root else Path(__file__).resolve().parent
    if not base_path.exists():
        return []
    return sorted([item.name for item in base_path.iterdir() if item.is_dir()])

def getPath(include_filename: bool = True) -> str:
    path = Path(__file__).resolve()
    return str(path if include_filename else path.parent)

# Datasets
paths: dict = {
    # Este diccionario almacenará la información del directorio protected y 
    # sus carpetas internas.
    # Root es la ruta raíz de la carpeta, y collecions son las carpetas contenidas
    "root": "",
    "collections": [],
}

# Performing
class FileManager:

    def __init__(self) -> None:
        self.root: Path = Path(getPath(include_filename=False)).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._refresh_paths()

    def _refresh_paths(self) -> None:
        paths["root"] = str(self.root)
        paths["collections"] = getFolders(self.root)

    def _coerce_target(self, target: "Collection | str | Path") -> Path:
        if isinstance(target, Collection):
            if not target.name:
                raise ValueError("Collection name cannot be empty.")
            target = target.name
        candidate = Path(target)
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        else:
            resolved = (self.root / candidate).resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("The target path must live inside the protected root.") from exc
        return resolved

    """INTERNAL API"""

    def getAllCollections(self, refresh: bool = False) -> list[str]:
        # Para consultar todas las colecciones
        if refresh:
            self._refresh_paths()
        return list(paths.get("collections", []))

    def createFolder(self, target: "Collection | str | Path", *, exist_ok: bool = False) -> str:
        destination = self._coerce_target(target)
        if destination.exists():
            if not destination.is_dir():
                raise FileExistsError(f"There is already a file at {destination}.")
            if not exist_ok:
                raise FileExistsError(f"The folder {destination.name} already exists.")
        else:
            destination.mkdir(parents=True)
        self._refresh_paths()
        return str(destination)

    def deleteFolder(
        self,
        target: "Collection | str | Path",
        *,
        recursive: bool = False,
        missing_ok: bool = False,
    ) -> bool:
        destination = self._coerce_target(target)
        if not destination.exists():
            if missing_ok:
                return False
            raise FileNotFoundError(f"The folder {destination} does not exist.")
        if not destination.is_dir():
            raise NotADirectoryError(f"{destination} is not a folder.")
        if any(destination.iterdir()):
            if not recursive:
                raise OSError(
                    f"The folder {destination} is not empty. Use recursive=True to remove it and its contents."
                )
            shutil.rmtree(destination)
        else:
            destination.rmdir()
        self._refresh_paths()
        return True

    def createFile(
        self,
        name: "Collection | str | Path",
        data: bytes | bytearray | memoryview | str | None = None,
        *,
        overwrite: bool = False,
    ) -> str:
        destination = self._coerce_target(name)
        if destination.exists():
            if destination.is_dir():
                raise IsADirectoryError(f"{destination} is a directory; cannot overwrite with a file.")
            if not overwrite:
                raise FileExistsError(f"The file {destination} already exists.")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
        if data is None:
            destination.touch(exist_ok=overwrite)
        elif isinstance(data, (bytes, bytearray, memoryview)):
            with destination.open("wb") as buffer:
                buffer.write(bytes(data))
        else:
            with destination.open("w", encoding="utf-8") as buffer:
                buffer.write(str(data))
        return str(destination)

    def deleteFile(self, name: "Collection | str | Path", *, missing_ok: bool = False) -> bool:
        destination = self._coerce_target(name)
        if not destination.exists():
            if missing_ok:
                return False
            raise FileNotFoundError(f"The file {destination} does not exist.")
        if destination.is_dir():
            raise IsADirectoryError(f"{destination} is a directory, not a file.")
        destination.unlink()
        return True

    pass


class ChatAttachmentManager(FileManager):
    """
    Gestor de archivos adjuntos asociados a chats.
    Valida extensiones y tipos MIME permitidos, normaliza nombres y
    almacena los archivos dentro de la colección `chats_files`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.collection: Path = Path("chats_files")
        self.allowed_extensions: dict[str, set[str]] = self._build_allowed_mime_map()

    @staticmethod
    def _build_allowed_mime_map() -> dict[str, set[str]]:
        fallback = {"application/octet-stream"}
        mapping: dict[str, Iterable[str]] = {
            ".jpg": {"image/jpeg"},
            ".jpeg": {"image/jpeg"},
            ".png": {"image/png"},
            ".zip": {"application/zip"},
            ".doc": {"application/msword"},
            ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
            ".ppt": {"application/vnd.ms-powerpoint"},
            ".pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
            ".xls": {"application/vnd.ms-excel"},
            ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            ".md": {"text/markdown", "text/plain"},
        }
        return {ext: set(types) | fallback for ext, types in mapping.items()}

    def _ensure_chat_folder(self, chat_id: int) -> Path:
        collection_path = self.collection / f"chat_{chat_id}"
        folder = self._coerce_target(collection_path)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _sanitize_basename(self, name: str) -> str:
        base = Path(name).stem or "file"
        sanitized = re.sub(r"[^A-Za-z0-9_\-]", "_", base)
        sanitized = sanitized.strip("_") or "file"
        return sanitized[:80]

    def _validate_extension(self, filename: str) -> str:
        extension = Path(filename or "").suffix.lower()
        if extension not in self.allowed_extensions:
            raise ValueError(
                f"Extensión de archivo no permitida: {extension or 'sin extensión'}."
            )
        return extension

    def _normalize_mime(self, filename: str, content_type: str | None) -> str:
        normalized = (content_type or "").lower().strip()
        if not normalized:
            guessed, _ = mimetypes.guess_type(filename)
            normalized = (guessed or "application/octet-stream").lower()
        return normalized

    def store_attachment(
        self,
        *,
        chat_id: int,
        sender_id: int,
        filename: str,
        payload: bytes,
        content_type: str | None = None,
    ) -> AttachmentInfo:
        if not payload:
            raise ValueError("El archivo está vacío.")
        extension = self._validate_extension(filename)
        mime_type = self._normalize_mime(filename, content_type)
        allowed_mimes = self.allowed_extensions[extension]
        if mime_type not in allowed_mimes:
            raise ValueError(
                f"Tipo MIME '{mime_type}' no coincide con la extensión {extension}."
            )

        folder = self._ensure_chat_folder(chat_id)
        basename = self._sanitize_basename(filename)
        unique_name = f"{basename}-{uuid4().hex[:8]}{extension}"
        destination = folder / unique_name

        with destination.open("wb") as buffer:
            buffer.write(payload)

        relative_path = str(Path(self.collection) / f"chat_{chat_id}" / unique_name)

        return AttachmentInfo(
            chat_id=chat_id,
            sender_id=sender_id,
            original_filename=filename,
            stored_filename=unique_name,
            relative_path=relative_path,
            absolute_path=str(destination.resolve()),
            mime_type=mime_type,
            size_bytes=len(payload),
        )

    def resolve_relative_path(self, relative_path: str) -> Path:
        return self._coerce_target(relative_path)

    def delete_attachment(self, relative_path: str) -> None:
        target = self.resolve_relative_path(relative_path)
        if target.exists():
            target.unlink()
            parent = target.parent
            try:
                if parent != self.root and parent.name.startswith("chat_") and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass


class ProfileImage(FileManager):

    def __init__(self):
        super().__init__()
        self.collection: str = "ProfileImages"
        self.allowed_formats: dict[str, str] = {
            ".jpg": "JPEG",
            ".jpeg": "JPEG",
            ".png": "PNG",
            ".webp": "WEBP",
        }
        self.default_extension: str = ".png"
        self.target_size: int = 512  # px

    """
    Métodos internos
    """

    def _ensure_pillow(self) -> None:
        if Image is None or ImageOps is None:
            raise RuntimeError(
                "Pillow es requerido para manejar imágenes de perfil. Instala con `pip install pillow`."
            )

    def _ensure_collection_folder(self) -> Path:
        collection_path = self.createFolder(self.collection, exist_ok=True)
        return Path(collection_path)

    def _infer_extension(self, source: Path | str | None) -> str | None:
        if not source:
            return None
        return Path(source).suffix.lower()

    def _build_filename(self, *, user_id: int | None, extension: str | None, filename: str | None) -> str:
        ext = (extension or "").lower()
        if ext not in self.allowed_formats:
            ext = self.default_extension
        base = Path(filename or "").stem
        base = re.sub(r"[^A-Za-z0-9_\-]", "_", base).strip("_")
        if not base:
            base = f"user_{user_id}" if user_id is not None else "profile"
        unique_suffix = uuid4().hex[:12]
        return f"{base[:80]}_{unique_suffix}{ext}"

    def _build_relative_path(self, filename: str) -> Path:
        return Path(self.collection) / filename

    def resolve_relative_path(self, relative_path: str) -> Path:
        if not relative_path:
            raise ValueError("No se proporcionó una ruta relativa.")
        return self._coerce_target(relative_path)

    def delete_profile_image(self, relative_path: str, *, missing_ok: bool = True) -> bool:
        target = self.resolve_relative_path(relative_path)
        if not target.exists():
            if missing_ok:
                return False
            raise FileNotFoundError(f"No existe la imagen de perfil en {target}.")
        if target.is_dir():
            raise IsADirectoryError(f"{target} es un directorio, no una imagen.")
        target.unlink()
        return True

    @contextmanager
    def _open_image(
        self, payload: bytes | bytearray | memoryview | io.BufferedIOBase | Path | str
    ):
        handle = None
        should_close = False
        if isinstance(payload, (bytes, bytearray, memoryview)):
            handle = io.BytesIO(payload)
        elif isinstance(payload, (str, Path)):
            path = Path(payload)
            if not path.exists():
                raise FileNotFoundError(f"No se encontró la imagen de origen: {path}")
            handle = path.open("rb")
            should_close = True
        elif hasattr(payload, "read"):
            handle = payload
        else:
            raise TypeError(
                "Payload inválido. Debe ser bytes, ruta a archivo o un objeto tipo archivo con método read()."
            )
        try:
            yield handle
        finally:
            if should_close and handle:
                handle.close()

    def _process_image(self, file_handle, size: int | tuple[int, int]) -> Image.Image:
        image = Image.open(file_handle)
        image.load()
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        if isinstance(size, int):
            target_size = (size, size)
        else:
            target_size = size
        # Ajusta la imagen para que quede cuadrada sin distorsión.
        return ImageOps.fit(image, target_size, Image.Resampling.LANCZOS, centering=(0.5, 0.5))

    """
    API 
    """ 

    def createProfileImage(
        self,
        payload: bytes | bytearray | memoryview | io.BufferedIOBase | Path | str,
        *,
        user_id: int | None = None,
        filename: str | None = None,
        size: int | tuple[int, int] | None = None,
        overwrite: bool = False,
    ) -> ProfileImageInfo:
        # recibe una imágen en formato .jpg, .jpeg, .png, .webp
        # Cambia el tamaño dejando una imagen cuadrada
        # Almacena la foto en su respectiva colección
        # Retorna el path del archivo
        self._ensure_pillow()
        size = size or self.target_size

        inferred_ext = self._infer_extension(filename) or self._infer_extension(
            payload if isinstance(payload, (str, Path)) else None
        )
        final_filename = self._build_filename(user_id=user_id, extension=inferred_ext, filename=filename)
        format_name = self.allowed_formats.get(Path(final_filename).suffix.lower(), self.allowed_formats[self.default_extension])

        self._ensure_collection_folder()
        relative_path = self._build_relative_path(final_filename)
        destination = self._coerce_target(relative_path)
        if destination.exists() and not overwrite:
            raise FileExistsError(f"Ya existe un archivo en {destination}")

        with self._open_image(payload) as source_image:
            profile_image = self._process_image(source_image, size)
            profile_image.save(destination, format=format_name)

        return ProfileImageInfo(
            user_id=user_id or -1,
            filename=final_filename,
            relative_path=relative_path.as_posix(),
            absolute_path=str(destination.resolve()),
        )

    pass

if __name__ == "__main__":
    #pat = FileManager()
    pass
