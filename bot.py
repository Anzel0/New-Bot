# =======================================================
# IMPORTACIONES
# =======================================================
import os
import psutil
import time
import asyncio
import logging
import urllib.parse
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified, FloodWait
import nest_asyncio
import cloudinary
import cloudinary.uploader

# =======================================================
# LÓGICA DE TU BOT
# =======================================================

# Aplicar nest_asyncio para entornos como Render
nest_asyncio.apply()

# --- Configuración de Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Constantes y Directorios ---
MAX_VIDEO_SIZE_MB = 4000
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Diccionario para almacenar el estado y datos por usuario
user_data = {}

# --- Configuración de Credenciales (Vía Variables de Entorno) ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET")

# Inicializar Cloudinary con tus credenciales
cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET
)

# --- Instancia del Bot ---
app = Client("video_processor_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Funciones de Utilidad ---
def format_size(size_bytes):
    if size_bytes is None: return "0 B"
    if size_bytes < 1024: return f"{size_bytes} Bytes"
    if size_bytes < 1024**2: return f"{size_bytes/1024:.2f} KB"
    if size_bytes < 1024**3: return f"{size_bytes/1024**2:.2f} MB"
    return f"{size_bytes/1024**3:.2f} GB"

def human_readable_time(seconds: int) -> str:
    if seconds is None: return "00:00"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

async def update_message(client, chat_id, message_id, text, reply_markup=None):
    """Edita un mensaje de forma segura."""
    try:
        await client.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup)
    except MessageNotModified:
        pass
    except FloodWait as e:
        logger.warning(f"FloodWait de {e.value}s. Esperando.")
        await asyncio.sleep(e.value)
        await update_message(client, chat_id, message_id, text, reply_markup)
    except Exception as e:
        logger.error(f"Error al actualizar mensaje: {e}")

def get_progress_bar(percentage):
    completed_blocks = int(percentage // 10)
    if percentage >= 100: return '■' * 10
    if completed_blocks < 10: return '■' * completed_blocks + '□' * (10 - completed_blocks)
    return '■' * 10

async def progress_bar_handler(current, total, client, message, start_time, action_text):
    """Manejador de progreso para Pyrogram con barra de bloques."""
    chat_id = message.chat.id
    user_info = user_data.get(chat_id, {})
    last_update_time = user_info.get('last_update_time', 0)
    current_time = time.time()

    if current_time - last_update_time < 3: return
    user_info['last_update_time'] = current_time

    percentage = (current * 100 / total) if total > 0 else 0
    elapsed_time = current_time - start_time
    speed = current / elapsed_time if elapsed_time > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0

    progress_bar = get_progress_bar(percentage)

    action_text_clean = action_text.replace('📥 Descargando', 'DESCARGANDO...').replace('⬆️ Subiendo', 'SUBIENDO...')

    text = (
        f"**{action_text_clean}**\n"
        f"`[{progress_bar}] {percentage:.1f}%`\n"
        f"\n"
        f"**Tamaño:** `{format_size(current)} / {format_size(total)}`\n"
        f"**Velocidad:** `{format_size(speed)}/s` | **ETA:** `{human_readable_time(eta)}`"
    )
    await update_message(client, chat_id, message.id, text)

# --- Lógica de Procesamiento de Video (con Cloudinary) ---
def build_cloudinary_transformation(options):
    """Construye el objeto de transformación de Cloudinary a partir de las opciones del usuario."""
    quality_option = options.get('quality', 'auto:low')
    resolution = options.get('resolution', '360')
    
    transformations = [
        {'quality': quality_option, 'fetch_format': 'auto'},
        {'height': resolution, 'crop': 'scale'},
    ]
    
    # Cloudinary no soporta 'preset' o 'fps' directamente en la transformación.
    # 'quality:auto' se encarga de la compresión.
    # La resolución es la única opción de tamaño que tiene sentido aplicar.
    
    return transformations

async def upload_and_compress_with_cloudinary(client, chat_id, status_message):
    """
    Descarga el video, lo sube a Cloudinary para compresión y devuelve la URL.
    Usa el manejador de progreso para la descarga.
    """
    user_info = user_data.get(chat_id)
    if not user_info:
        return None, None

    # Descargar el video de Telegram
    await status_message.edit_text("⏳ Descargando video de Telegram...")
    start_time = time.time()
    file_path = await client.download_media(
        message=await client.get_messages(chat_id, user_info['original_message_id']),
        file_name=os.path.join(DOWNLOAD_DIR, f"{chat_id}_{user_info['video_file_name']}"),
        progress=progress_bar_handler,
        progress_args=(client, status_message, start_time, "📥 Descargando")
    )
    if not file_path or not os.path.exists(file_path):
        await status_message.edit_text("❌ Error en la descarga del video.")
        return None, None

    # Subir y comprimir con Cloudinary
    await status_message.edit_text("🔄 Subiendo y comprimiendo con Cloudinary...")
    try:
        options = user_info.get('compression_options', {})
        transformations = build_cloudinary_transformation(options)
        
        upload_result = cloudinary.uploader.upload(
            file_path,
            resource_type="video",
            transformation=transformations
        )
        compressed_url = upload_result['secure_url']
        original_size = os.path.getsize(file_path)
        
        return compressed_url, original_size
    except Exception as e:
        logger.error(f"Error al subir a Cloudinary: {e}", exc_info=True)
        await status_message.edit_text("❌ Error al subir y comprimir el video en Cloudinary.")
        return None, None
    finally:
        # Limpiar el archivo descargado localmente
        if os.path.exists(file_path):
            os.remove(file_path)

async def upload_final_video(client, chat_id, url_or_path, original_size=None):
    """Sube el video procesado final a Telegram."""
    user_info = user_data.get(chat_id)
    if not user_info: return

    status_id = user_info['status_message_id']
    status_message = await client.get_messages(chat_id, status_id)
    video_source = url_or_path
    
    final_filename = user_info.get('new_name') or os.path.basename(user_info['video_file_name'])
    if user_info.get('new_name') and not final_filename.endswith(".mp4"):
        final_filename += ".mp4"

    try:
        start_time = time.time()
        await update_message(client, chat_id, status_id, "⬆️ SUBIENDO...")

        if user_info.get('send_as_file'):
            await client.send_document(
                chat_id=chat_id, document=video_source, thumb=user_info.get('thumbnail_path'),
                file_name=final_filename, caption=f"`{final_filename}`",
                progress=progress_bar_handler, progress_args=(client, status_message, start_time, "⬆️ Subiendo")
            )
        else:
            await client.send_video(
                chat_id=chat_id, video=video_source, caption=f"`{final_filename}`",
                thumb=user_info.get('thumbnail_path'), supports_streaming=True,
                progress=progress_bar_handler,
                progress_args=(client, status_message, start_time, "⬆️ Subiendo")
            )

        await status_message.delete()
        if original_size:
            caption_text = f"✅ ¡Proceso completado!\n\n**Tamaño Original:** `{format_size(original_size)}`"
            await client.send_message(chat_id, caption_text)
        else:
            await client.send_message(chat_id, "✅ ¡Proceso completado!")
    except Exception as e:
        logger.error(f"Error al subir para {chat_id}: {e}", exc_info=True)
        await update_message(client, chat_id, status_id, f"❌ Error durante la subida.")
    finally:
        clean_up(chat_id)


# --- Handlers de Mensajes y Callbacks ---
@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    clean_up(message.chat.id)
    await message.reply(
        "¡Hola! 👋 Soy tu bot para procesar videos.\n\n"
        "Puedo **comprimir** y **convertir** tus videos. **Envíame un video para empezar.**"
    )

@app.on_message(filters.video & filters.private)
async def video_handler(client, message: Message):
    chat_id = message.chat.id
    if user_data.get(chat_id):
        await client.send_message(chat_id, "⚠️ Un proceso anterior se ha cancelado para iniciar uno nuevo.")
        clean_up(chat_id)
        
    if message.video.file_size > MAX_VIDEO_SIZE_MB * 1024 * 1024:
        await message.reply(f"❌ El video supera el límite de {MAX_VIDEO_SIZE_MB} MB.")
        return

    user_data[chat_id] = {
        'state': 'awaiting_action',
        'original_message_id': message.id,
        'video_file_name': message.video.file_name or f"video_{message.video.file_id}.mp4",
        'last_update_time': 0,
    }

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗜️ Comprimir Video", callback_data="action_compress")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")]
    ])
    await message.reply_text("Video recibido. ¿Qué quieres hacer?", reply_markup=keyboard, quote=True)

@app.on_message(filters.photo & filters.private)
async def thumbnail_handler(client, message: Message):
    chat_id = message.chat.id
    user_info = user_data.get(chat_id)
    if not user_info or user_info.get('state') != 'waiting_for_thumbnail':
        return

    status_id = user_info['status_message_id']
    await update_message(client, chat_id, status_id, "🖼️ Descargando miniatura...")

    try:
        thumb_path = await client.download_media(message=message, file_name=os.path.join(DOWNLOAD_DIR, f"thumb_{chat_id}.jpg"))
        user_info['thumbnail_path'] = thumb_path
        await show_rename_options(client, chat_id, status_id, "Miniatura guardada. ¿Quieres renombrar el video?")
    except Exception as e:
        logger.error(f"Error al descargar la miniatura: {e}")
        await update_message(client, chat_id, status_id, "❌ Error al descargar la miniatura.")
        clean_up(chat_id)

@app.on_message(filters.text & filters.private)
async def rename_handler(client, message: Message):
    chat_id = message.chat.id
    user_info = user_data.get(chat_id)
    if not user_info or user_info.get('state') != 'waiting_for_new_name':
        return

    user_info['new_name'] = message.text.strip()
    await message.delete()
    status_id = user_info['status_message_id']
    await update_message(client, chat_id, status_id, f"✅ Nombre guardado. Preparando para subir...")
    user_info['state'] = 'uploading'
    await upload_final_video(client, chat_id, user_info['final_url_or_path'], user_info.get('original_size'))

@app.on_callback_query()
async def callback_handler(client, cb: CallbackQuery):
    chat_id = cb.message.chat.id
    user_info = user_data.get(chat_id)
    if not user_info:
        await cb.answer("Esta operación ha expirado.", show_alert=True)
        await cb.message.delete()
        return

    action = cb.data
    user_info['status_message_id'] = cb.message.id
    await cb.answer()

    if action == "cancel":
        user_info['state'] = 'cancelled'
        await cb.message.edit("Operación cancelada.")
        clean_up(chat_id)

    elif action == "action_compress":
        await show_compression_options(client, chat_id, cb.message.id)

    elif action == "compressopt_default":
        user_info['compression_options'] = {'quality': 'auto:low', 'resolution': '360'}
        await cb.message.edit("Iniciando compresión con opciones por defecto...")
        compressed_url, original_size = await upload_and_compress_with_cloudinary(client, chat_id, cb.message)
        if compressed_url:
            user_info['final_url_or_path'] = compressed_url
            user_info['original_size'] = original_size
            summary = (f"✅ **Compresión Exitosa**\n\n"
                       f"**📏 Original:** `{format_size(original_size)}`\n"
                       f"Ahora, ¿cómo quieres continuar?")
            await show_conversion_options(client, chat_id, cb.message.id, text=summary)
        else:
            await cb.message.edit("❌ Error en la compresión. Operación cancelada.")
            clean_up(chat_id)

    elif action == "compressopt_advanced":
        user_info['compression_options'] = {} # Reinicia las opciones para la configuración avanzada
        await show_advanced_menu(client, chat_id, cb.message.id, "quality")

    elif action.startswith("adv_"):
        part, value = action.split("_")[1], action.split("_")[2]
        user_info.setdefault('compression_options', {})[part] = value
        
        next_part_map = {"quality": "resolution", "resolution": "confirm"}
        next_part = next_part_map.get(part)
        
        if next_part:
            await show_advanced_menu(client, chat_id, cb.message.id, next_part, user_info['compression_options'])
        else:
             await show_advanced_menu(client, chat_id, cb.message.id, "confirm", user_info['compression_options'])

    elif action == "start_advanced_compression":
        await cb.message.edit("Opciones guardadas. Iniciando compresión...")
        compressed_url, original_size = await upload_and_compress_with_cloudinary(client, chat_id, cb.message)
        if compressed_url:
            user_info['final_url_or_path'] = compressed_url
            user_info['original_size'] = original_size
            summary = (f"✅ **Compresión Exitosa**\n\n"
                       f"**📏 Original:** `{format_size(original_size)}`\n"
                       f"Ahora, ¿cómo quieres continuar?")
            await show_conversion_options(client, chat_id, cb.message.id, text=summary)
        else:
            await cb.message.edit("❌ Error en la compresión. Operación cancelada.")
            clean_up(chat_id)
            
    elif action == "convertopt_withthumb":
        user_info['state'] = 'waiting_for_thumbnail'
        await cb.message.edit("Por favor, envía la imagen para la miniatura.")

    elif action == "convertopt_nothumb":
        user_info['thumbnail_path'] = None
        await show_rename_options(client, chat_id, cb.message.id)

    elif action == "convertopt_asfile":
        user_info['send_as_file'] = True
        await show_rename_options(client, chat_id, cb.message.id)

    elif action == "renameopt_yes":
        user_info['state'] = 'waiting_for_new_name'
        await cb.message.edit("Ok, envíame el nuevo nombre (sin extensión).")

    elif action == "renameopt_no":
        user_info['new_name'] = None
        user_info['state'] = 'uploading'
        await cb.message.edit("Entendido. Preparando para subir...")
        await upload_final_video(client, chat_id, user_info['final_url_or_path'], user_info.get('original_size'))

# --- Funciones de Menús ---
async def show_compression_options(client, chat_id, msg_id):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Usar Opciones Recomendadas", callback_data="compressopt_default")],
        [InlineKeyboardButton("⚙️ Configurar Opciones Avanzadas", callback_data="compressopt_advanced")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")]
    ])
    await update_message(client, chat_id, msg_id, "Elige cómo quieres comprimir:", reply_markup=keyboard)

async def show_advanced_menu(client, chat_id, msg_id, part, opts=None):
    menus = {
        "quality": {"text": "1/2: Calidad (RFC)", "opts": [("18", "auto:18"), ("22", "auto:22"), ("28", "auto:28")], "prefix": "adv_quality"},
        "resolution": {"text": "2/2: Resolución", "opts": [("240p", "240"), ("360p", "360"), ("480p", "480"), ("720p", "720"), ("1080p", "1080")], "prefix": "adv_resolution"},
    }
    if part == "confirm":
        text = (f"Confirmar opciones:\n"
                f"- Calidad (RFC): `{opts.get('quality', 'N/A').split(':')[-1]}`\n"
                f"- Resolución: `{opts.get('resolution', 'N/A')}p`\n"
                f"\n¿Estás listo para iniciar la compresión?")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Iniciar Compresión", callback_data="start_advanced_compression")]])
    else:
        info = menus[part]
        buttons = [InlineKeyboardButton(text, callback_data=f"{info['prefix']}_{val}") for text, val in info["opts"]]
        keyboard = InlineKeyboardMarkup([buttons])
        text = info["text"]
    await update_message(client, chat_id, msg_id, text, reply_markup=keyboard)

async def show_conversion_options(client, chat_id, msg_id, text="¿Cómo quieres enviar el video?"):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Con Miniatura", callback_data="convertopt_withthumb")],
        [InlineKeyboardButton("🚫 Sin Miniatura", callback_data="convertopt_nothumb")],
        [InlineKeyboardButton("📂 Enviar como Archivo", callback_data="convertopt_asfile")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")]
    ])
    await update_message(client, chat_id, msg_id, text, reply_markup=keyboard)

async def show_rename_options(client, chat_id, msg_id, text="¿Quieres renombrar el archivo?"):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Sí, renombrar", callback_data="renameopt_yes")],
        [InlineKeyboardButton("➡️ No, usar original", callback_data="renameopt_no")]
    ])
    await update_message(client, chat_id, msg_id, text, reply_markup=keyboard)

# --- Limpieza y Arranque ---
def clean_up(chat_id):
    user_info = user_data.pop(chat_id, None)
    if not user_info: return
    for key in ['download_path', 'thumbnail_path', 'final_path']:
        path = user_info.get(key)
        if path and os.path.exists(path):
            try: os.remove(path)
            except OSError as e: logger.warning(f"No se pudo eliminar {path}: {e}")
    logger.info(f"Datos del usuario {chat_id} limpiados.")

# --- Funciones de Arranque ---
async def main():
    logger.info("Iniciando bot...")
    await app.start()
    me = await app.get_me()
    logger.info(f"Bot en línea como @{me.username}.")
    while True:
        await asyncio.sleep(60)

if __name__ == "__main__":
    try:
        app.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot detenido manualmente.")
