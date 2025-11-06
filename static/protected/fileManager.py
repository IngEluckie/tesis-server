# fileManager.py

"""
Bodyguard for all protected files
"""

# Import libreries
from dataclasses import dataclass
from pathlib import Path
import shutil
import io
from uuid import uuid4
from contextlib import contextmanager

from PIL import Image, ImageOps

# Import modules

# Global variables and functions
@dataclass
class ProfileImageInfo:
    user_id: int
    filename: str
    path: str


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
        if filename:
            candidate = filename
            if Path(candidate).suffix.lower() not in self.allowed_formats:
                candidate += self.default_extension
            return candidate
        if ext not in self.allowed_formats:
            ext = self.default_extension
        base = f"user_{user_id}" if user_id is not None else "profile"
        return f"{base}_{uuid4().hex}{ext}"

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
        destination = self._coerce_target(Path(self.collection) / final_filename)
        if destination.exists() and not overwrite:
            raise FileExistsError(f"Ya existe un archivo en {destination}")

        with self._open_image(payload) as source_image:
            profile_image = self._process_image(source_image, size)
            profile_image.save(destination, format=format_name)

        return ProfileImageInfo(
            user_id=user_id or -1,
            filename=final_filename,
            path=str(destination),
        )

    pass

if __name__ == "__main__":
    #pat = FileManager()
    pass