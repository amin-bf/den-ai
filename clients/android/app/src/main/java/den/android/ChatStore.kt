package den.android

import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.provider.MediaStore
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * A picture in a conversation: the file it lives in here on the phone, and the id it is known by
 * for as long as the conversation lasts. Ids are what the user and the model call a picture —
 * ordinals shift as a conversation grows, an id never does.
 */
class Picture(val id: String, val file: String) {
    fun toJson(): JSONObject = JSONObject().put("id", id).put("file", file)

    companion object {
        fun fromJson(o: JSONObject) = Picture(o.text("id"), o.text("file"))
    }
}

/**
 * One message of a chat. Its pictures live in files inside the conversation's own folder here on
 * the phone; what den generated is also in the gallery, under the conversation's folder.
 */
class ChatMessage(
    val role: String,
    var content: String = "",
    /** What the model thought on the way to this answer, when thinking was on. */
    var thinking: String = "",
    var toolCalls: JSONArray? = null,
    val toolCallId: String? = null,
    val toolName: String? = null,
    val pictures: MutableList<Picture> = mutableListOf(),
    var note: String? = null,
) {
    /** The files of this message's pictures, in order. */
    val images: List<String> get() = pictures.map { it.file }

    fun toJson(): JSONObject = JSONObject()
        .put("role", role)
        .put("content", content)
        .put("thinking", thinking)
        .put("tool_calls", toolCalls ?: JSONObject.NULL)
        .put("tool_call_id", toolCallId ?: JSONObject.NULL)
        .put("name", toolName ?: JSONObject.NULL)
        .put("pictures", JSONArray(pictures.map { it.toJson() }))
        .put("note", note ?: JSONObject.NULL)

    /** The message as /v1/chat/completions takes it: no images, no notes of our own. */
    fun toApi(): JSONObject {
        val message = JSONObject().put("role", role).put("content", content)
        toolCalls?.let { message.put("tool_calls", it) }
        toolCallId?.let { message.put("tool_call_id", it) }
        toolName?.let { message.put("name", it) }
        return message
    }

    companion object {
        fun fromJson(o: JSONObject) = ChatMessage(
            role = o.text("role"),
            content = o.text("content"),
            thinking = o.text("thinking"),
            toolCalls = o.optJSONArray("tool_calls"),
            toolCallId = o.textOrNull("tool_call_id"),
            toolName = o.textOrNull("name"),
            pictures = o.optJSONArray("pictures")?.let { a ->
                (0 until a.length()).map { Picture.fromJson(a.getJSONObject(it)) }
            }.orEmpty().toMutableList(),
            note = o.textOrNull("note"),
        )

        /** A conversation stored before pictures had ids: its files are given ids on the way in. */
        fun legacyFiles(o: JSONObject): List<String> =
            o.optJSONArray("images")?.let { a -> (0 until a.length()).map { a.getString(it) } }.orEmpty()
    }
}

/**
 * A skill (or one of its reference files) from the broker's den, loaded because the user picked
 * it, and kept with the conversation it was picked for.
 */
class Attachment(val name: String, val reference: String?, val text: String) {
    val label: String get() = if (reference == null) name else "$name · $reference"

    /** Roughly what it costs in the model's context; bytes/4 is close enough to show. */
    val tokens: Int get() = text.length / 4

    fun toJson(): JSONObject = JSONObject()
        .put("name", name).put("reference", reference ?: JSONObject.NULL).put("text", text)

    companion object {
        fun fromJson(o: JSONObject) = Attachment(
            o.text("name"), o.textOrNull("reference"), o.text("text"),
        )
    }
}

/** A conversation: its messages, the skills loaded into it, and the gallery folder for images. */
class Conversation(
    val id: String,
    var title: String,
    var folder: String? = null,
    /** The model this conversation asks for, or null for the one the den has selected. */
    var model: String? = null,
    val messages: MutableList<ChatMessage> = mutableListOf(),
    val attachments: MutableList<Attachment> = mutableListOf(),
    var updated: Long = System.currentTimeMillis(),
    /** The number the next picture of this conversation gets; ids are never reused. */
    private var nextPicture: Int = 1,
) {
    /** The id for a new picture here: img-1, img-2, … counting up and never going back. */
    fun newPictureId(): String = "img-${nextPicture++}"

    /** Every picture of this conversation, oldest first, whichever message it belongs to. */
    fun pictures(): List<Picture> = messages.flatMap { it.pictures }

    fun picture(id: String): Picture? = pictures().firstOrNull { it.id == id }

    fun toJson(): JSONObject = JSONObject()
        .put("id", id)
        .put("title", title)
        .put("folder", folder ?: JSONObject.NULL)
        .put("model", model ?: JSONObject.NULL)
        .put("next_picture", nextPicture)
        .put("updated", updated)
        .put("messages", JSONArray(messages.map { it.toJson() }))
        .put("attachments", JSONArray(attachments.map { it.toJson() }))

    companion object {
        fun fromJson(o: JSONObject): Conversation {
            val stored = o.optJSONArray("messages")
            val messages = (0 until (stored?.length() ?: 0)).map { ChatMessage.fromJson(stored!!.getJSONObject(it)) }
            val conversation = Conversation(
                id = o.getString("id"),
                title = o.text("title"),
                folder = o.textOrNull("folder"),
                model = o.textOrNull("model"),
                messages = messages.toMutableList(),
                attachments = o.optJSONArray("attachments")?.let { a ->
                    (0 until a.length()).map { Attachment.fromJson(a.getJSONObject(it)) }
                }.orEmpty().toMutableList(),
                updated = o.optLong("updated"),
                nextPicture = o.optInt("next_picture", 1),
            )
            // A conversation stored before ids: give its pictures ids once, in their own order.
            messages.forEachIndexed { at, message ->
                if (message.pictures.isEmpty()) {
                    ChatMessage.legacyFiles(stored!!.getJSONObject(at)).forEach { file ->
                        message.pictures.add(Picture(conversation.newPictureId(), file))
                    }
                }
            }
            return conversation
        }
    }
}

/** Conversations kept on the phone, one folder each, with their images beside them. */
class ChatStore(private val context: Context) {
    private val root = File(context.filesDir, "chats").apply { mkdirs() }

    fun list(): List<Conversation> = root.listFiles { f -> f.name.endsWith(".json") }
        .orEmpty()
        .mapNotNull { runCatching { Conversation.fromJson(JSONObject(it.readText())) }.getOrNull() }
        .sortedByDescending { it.updated }

    fun create(): Conversation = Conversation(id = SimpleDateFormat("yyyyMMdd-HHmmss-SSS", Locale.ROOT).format(Date()), title = "New chat")

    fun save(conversation: Conversation) {
        conversation.updated = System.currentTimeMillis()
        File(root, "${conversation.id}.json").writeText(conversation.toJson().toString())
    }

    fun delete(conversation: Conversation, withImages: Boolean) {
        File(root, "${conversation.id}.json").delete()
        folderOf(conversation).deleteRecursively()
        if (withImages) deleteFromGallery(conversation)
    }

    fun folderOf(conversation: Conversation) = File(root, conversation.id)

    fun imageFile(conversation: Conversation, name: String) = File(folderOf(conversation), name)

    /** How many of this conversation's images the gallery still holds. */
    fun galleryCount(conversation: Conversation): Int {
        val folder = conversation.folder ?: return 0
        return context.contentResolver.query(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI, arrayOf(MediaStore.Images.Media._ID),
            "${MediaStore.Images.Media.RELATIVE_PATH} LIKE ?", arrayOf("$GALLERY_ROOT$folder/%"), null,
        )?.use { it.count } ?: 0
    }

    private fun deleteFromGallery(conversation: Conversation) {
        val folder = conversation.folder ?: return
        val resolver = context.contentResolver
        resolver.query(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI, arrayOf(MediaStore.Images.Media._ID),
            "${MediaStore.Images.Media.RELATIVE_PATH} LIKE ?", arrayOf("$GALLERY_ROOT$folder/%"), null,
        )?.use { cursor ->
            while (cursor.moveToNext()) {
                val uri = ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, cursor.getLong(0))
                runCatching { resolver.delete(uri, null, null) }
            }
        }
    }

    /** Keeps an image the user attached to a message, beside its conversation (not in the gallery). */
    fun saveAttachment(conversation: Conversation, attached: Attached): Picture {
        folderOf(conversation).mkdirs()
        val name = freeName(conversation, "sent-", ".jpg")
        imageFile(conversation, name).writeBytes(attached.jpeg)
        return Picture(conversation.newPictureId(), name)
    }

    /**
     * A name no file in this conversation has yet. Several pictures attached to one message are
     * saved in the same millisecond, and a name from the clock alone would have them overwrite
     * each other — every one of them would then be the last.
     */
    private fun freeName(conversation: Conversation, prefix: String, suffix: String): String {
        val stamp = SimpleDateFormat("yyyyMMdd-HHmmss-SSS", Locale.ROOT).format(Date())
        var name = "$prefix$stamp$suffix"
        var next = 2
        while (imageFile(conversation, name).exists()) {
            name = "$prefix$stamp-$next$suffix"
            next++
        }
        return name
    }

    fun bytesOf(conversation: Conversation, name: String): ByteArray? =
        imageFile(conversation, name).takeIf { it.isFile }?.readBytes()

    /**
     * Keeps one generated image with its conversation and in the gallery, under the
     * conversation's own folder. Returns the picture (its id and file) and its gallery path.
     */
    fun saveImage(conversation: Conversation, png: ByteArray): Pair<Picture, String> {
        if (conversation.folder == null) conversation.folder = folderName(conversation)
        folderOf(conversation).mkdirs()
        val name = freeName(conversation, "den-", ".png")
        imageFile(conversation, name).writeBytes(png)

        val relative = "$GALLERY_ROOT${conversation.folder}/"
        val values = ContentValues().apply {
            put(MediaStore.Images.Media.DISPLAY_NAME, name)
            put(MediaStore.Images.Media.MIME_TYPE, "image/png")
            put(MediaStore.Images.Media.RELATIVE_PATH, relative)
            put(MediaStore.Images.Media.IS_PENDING, 1)
        }
        val resolver = context.contentResolver
        val uri = resolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values)
            ?: throw DenException("could not save the image to the gallery")
        resolver.openOutputStream(uri)!!.use { it.write(png) }
        values.clear()
        values.put(MediaStore.Images.Media.IS_PENDING, 0)
        resolver.update(uri, values, null, null)
        return Picture(conversation.newPictureId(), name) to "$relative$name"
    }

    /** A folder name from the title: safe, short and unique to the conversation. */
    private fun folderName(conversation: Conversation): String {
        val slug = conversation.title.lowercase()
            .map { if (it.isLetterOrDigit()) it else '-' }
            .joinToString("")
            .split("-").filter { it.isNotEmpty() }
            .joinToString("-")
            .take(40)
            .trim('-')
        return listOf(slug.ifEmpty { "chat" }, conversation.id).joinToString("-")
    }

    companion object {
        const val GALLERY_ROOT = "Pictures/den/chat/"
    }
}
