import os
import psutil
import time
import asyncio
import logging
import subprocess
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified, FloodWait
import nest_asyncio

# Aplicar nest_asyncio para entornos como Jupyter Notebook o Render
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

# --- Instancia del Bot ---
app = Client("video_processor_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Funciones de Utilidad ---
def format_size(size_bytes):
    if size_bytes is None: return "0 B"
    if size_bytes < 1024: return f"{size_bytes} Bytes"
    if size_bytes < 1024**2: return f"{size_bytes/1024:.2f} KB"
    if size_bytes < 1024**3: return f"{size_bytes/1024**2:.2f} MB"
    return f"{size_bytes/1024**3:.2f} GB"

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
    progress_bar = get_progress_bar(percentage)

    text = (
        f"**{action_text}**\n"
        f"`[{progress_bar}] {percentage:.1f}%`\n"
        f"\n"
        f"**Tamaño:** `{format_size(current)} / {format_size(total)}`"
    )
    await update_message(client, chat_id, message.id, text)

# --- Lógica de Procesamiento de Video (con FFmpeg) ---

async def compress_video_with_ffmpeg(client, chat_id, status_message):
    """Descarga el video, lo comprime con FFmpeg y devuelve la ruta del archivo comprimido."""
    user_info = user_data.get(chat_id)
    if not user_info: return None, None

    # Descargar el video de Telegram
    start_time = time.time()
    await status_message.edit_text("⏳ Descargando video de Telegram...")
    file_path = await client.download_media(
        message=await client.get_messages(chat_id, user_info['original_message_id']),
        file_name=os.path.join(DOWNLOAD_DIR, f"{chat_id}_{user_info['video_file_name']}"),
        progress=progress_bar_handler, progress_args=(client, status_message, start_time, "📥 Descargando")
    )
    if not file_path or not os.path.exists(file_path):
        await status_message.edit_text("❌ Error en la descarga del video.")
        return None, None

    # Comprimir video con FFmpeg
    await status_message.edit_text("🔄 Comprimiendo video con FFmpeg...")
    original_size = os.path.getsize(file_path)
    compressed_file_path = os.path.join(DOWNLOAD_DIR, f"compressed_{chat_id}.mp4")

    # Comando de FFmpeg con los parámetros solicitados
    command = [
        'ffmpeg',
        '-i', file_path,
        '-vf', 'scale=-2:360',  # Resolución: 360p, ancho automático
        '-crf', '22',           # Calidad: CRF 22 (más bajo, mejor calidad, más grande)
        '-r', '30',             # FPS: 30
        '-b:a', '64k',          # Bitrate de audio: 64k
        '-c:v', 'libx264',      # Codec de video (h264)
        '-c:a', 'aac',          # Codec de audio (aac)
        '-movflags', '+faststart', # Optimiza para streaming
        '-y',                   # Sobrescribe el archivo de salida si existe
        compressed_file_path
    ]

    try:
        # Usamos asyncio.create_subprocess_exec para no bloquear el bot
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            error_msg = stderr.decode().strip()
            logger.error(f"Error en FFmpeg: {error_msg}")
            raise Exception(f"Fallo la compresión. Error: {error_msg}")
        
        return compressed_file_path, original_size
    except Exception as e:
        logger.error(f"Error en el proceso de FFmpeg: {e}", exc_info=True)
        await status_message.edit_text(f"❌ Error al comprimir el video: {e}")
        return None, None
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

async def upload_final_video(client, chat_id, final_video_path, original_size=None):
    """Sube el video comprimido a Telegram."""
    user_info = user_data.get(chat_id)
    if not user_info: return

    status_id = user_info['status_message_id']
    status_message = await client.get_messages(chat_id, status_id)
    
    try:
        # Subimos el video a Telegram
        start_time = time.time()
        await update_message(client, chat_id, status_id, "⬆️ SUBIENDO...")

        await client.send_video(
            chat_id=chat_id, 
            video=final_video_path,
            caption=f"✅ Video comprimido",
            supports_streaming=True,
            progress=progress_bar_handler,
            progress_args=(client, status_message, start_time, "⬆️ Subiendo")
        )

        await status_message.delete()
        if original_size:
            final_size = os.path.getsize(final_video_path)
            caption_text = f"✅ ¡Proceso completado!\n\n**Tamaño Original:** `{format_size(original_size)}`\n**Tamaño Final:** `{format_size(final_size)}`"
            await client.send_message(chat_id, caption_text)
        else:
            await client.send_message(chat_id, "✅ ¡Proceso completado!")
    except Exception as e:
        logger.error(f"Error en el proceso para {chat_id}: {e}", exc_info=True)
        await update_message(client, chat_id, status_id, f"❌ Error durante el proceso.")
    finally:
        if final_video_path and os.path.exists(final_video_path):
            os.remove(final_video_path)
        clean_up(chat_id)

# --- Handlers de Mensajes y Callbacks ---
@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    clean_up(message.chat.id)
    await message.reply(
        "¡Hola! 👋 Soy tu bot para comprimir videos.\n\n"
        "**Envíame un video para empezar.**"
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
        await cb.message.edit("Iniciando el proceso de compresión...")
        compressed_file_path, original_size = await compress_video_with_ffmpeg(client, chat_id, cb.message)
        if compressed_file_path:
            await upload_final_video(client, chat_id, compressed_file_path, original_size)
        else:
            await cb.message.edit("❌ Error en la compresión. Operación cancelada.")
            clean_up(chat_id)

# --- Limpieza y Arranque ---
def clean_up(chat_id):
    user_info = user_data.pop(chat_id, None)
    if not user_info: return
    for key in ['download_path', 'final_path']:
        path = user_info.get(key)
        if path and os.path.exists(path):
            try: os.remove(path)
            except OSError as e: logger.warning(f"No se pudo eliminar {path}: {e}")
    logger.info(f"Datos del usuario {chat_id} limpiados.")

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
