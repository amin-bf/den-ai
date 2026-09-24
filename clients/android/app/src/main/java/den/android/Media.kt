package den.android

import android.content.ContentUris
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.net.Uri
import android.provider.MediaStore
import androidx.core.content.FileProvider
import androidx.exifinterface.media.ExifInterface
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File

/** An image the user attached to the message being written, already scaled down for the model. */
class Attached(val name: String, val jpeg: ByteArray, val fromWidth: Int, val fromHeight: Int, val width: Int, val height: Int) {
    val scaled: Boolean get() = width < fromWidth || height < fromHeight
    val bitmap: Bitmap? by lazy { BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size) }
    val kb: Int get() = jpeg.size / 1024
}

/** Pictures on the way to the model, and the phone's own ways of showing or sharing one. */
object Media {
    /** The model sees a picture, not a photograph: a long side of this is plenty and cheap. */
    const val LONG_SIDE = 1024
    const val QUALITY = 85
    const val MIME = "image/jpeg"

    /** Reads a picked image and scales it to [LONG_SIDE] as JPEG, keeping its proportions. */
    fun attach(context: Context, uri: Uri, name: String): Attached? {
        val bytes = context.contentResolver.openInputStream(uri)?.use { it.readBytes() } ?: return null
        return scale(bytes, name.substringAfterLast('/'))
    }

    fun scale(bytes: ByteArray, name: String): Attached? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        val (width, height) = bounds.outWidth to bounds.outHeight
        if (width <= 0 || height <= 0) return null
        // Decode at a power-of-two step first, so a 12 MP photo never becomes a full-size bitmap.
        val options = BitmapFactory.Options().apply {
            inSampleSize = generateSequence(1) { it * 2 }.first { maxOf(width, height) / (it * 2) < LONG_SIDE }
        }
        val decoded = BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options) ?: return null
        // A camera writes which way up it was held into the EXIF instead of turning the pixels.
        // Turn them here, so the copy kept, the thumbnail and what the model sees all stand up.
        val upright = turn(decoded, orientationOf(bytes))
        val longest = maxOf(upright.width, upright.height)
        val bitmap = if (longest <= LONG_SIDE) upright else {
            val ratio = LONG_SIDE.toFloat() / longest
            upright.scale((upright.width * ratio).toInt(), (upright.height * ratio).toInt())
        }
        val out = ByteArrayOutputStream()
        // Re-encoded from pixels, so the result carries no orientation tag of its own.
        bitmap.compress(Bitmap.CompressFormat.JPEG, QUALITY, out)
        val from = if (orientationOf(bytes).quarterTurns % 2 == 1) height to width else width to height
        return Attached(
            name = name.substringBeforeLast('.').ifEmpty { "image" } + ".jpg",
            jpeg = out.toByteArray(),
            fromWidth = from.first, fromHeight = from.second, width = bitmap.width, height = bitmap.height,
        )
    }

    /** How a picture has to be turned to stand up, as its EXIF orientation tag says. */
    data class Upright(val quarterTurns: Int, val mirrored: Boolean) {
        val degrees: Float get() = (quarterTurns * 90).toFloat()
    }

    /**
     * The EXIF orientation tag as turns and a mirror. Pure, so it can be checked without a
     * device: the tags are the eight of the standard, 1 being "already upright".
     */
    fun upright(tag: Int): Upright = when (tag) {
        ExifInterface.ORIENTATION_ROTATE_90 -> Upright(1, false)
        ExifInterface.ORIENTATION_ROTATE_180 -> Upright(2, false)
        ExifInterface.ORIENTATION_ROTATE_270 -> Upright(3, false)
        ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> Upright(0, true)
        ExifInterface.ORIENTATION_FLIP_VERTICAL -> Upright(2, true)
        ExifInterface.ORIENTATION_TRANSPOSE -> Upright(3, true)
        ExifInterface.ORIENTATION_TRANSVERSE -> Upright(1, true)
        else -> Upright(0, false)
    }

    /** What the picture's own EXIF says, or upright when it says nothing. */
    fun orientationOf(bytes: ByteArray): Upright = runCatching {
        upright(
            ExifInterface(ByteArrayInputStream(bytes))
                .getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)
        )
    }.getOrDefault(Upright(0, false))

    private fun turn(bitmap: Bitmap, upright: Upright): Bitmap {
        if (upright.quarterTurns == 0 && !upright.mirrored) return bitmap
        val matrix = Matrix().apply {
            if (upright.mirrored) postScale(-1f, 1f)
            if (upright.quarterTurns != 0) postRotate(upright.degrees)
        }
        return Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true)
    }

    private fun Bitmap.scale(width: Int, height: Int): Bitmap = Bitmap.createScaledBitmap(this, width, height, true)

    /** What an OpenAI-style image part carries. */
    fun dataUrl(jpeg: ByteArray): String =
        "data:$MIME;base64," + android.util.Base64.encodeToString(jpeg, android.util.Base64.NO_WRAP)

    /** A content URI other apps may read, for a file this app keeps. */
    fun share(context: Context, file: File): Uri =
        FileProvider.getUriForFile(context, "${context.packageName}.files", file)

    /** The gallery's own URI for a picture den saved there, from the path a result names. */
    fun inGallery(context: Context, relativePath: String): Uri? {
        val folder = relativePath.substringBeforeLast('/') + "/"
        val name = relativePath.substringAfterLast('/')
        return context.contentResolver.query(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
            arrayOf(MediaStore.Images.Media._ID),
            "${MediaStore.Images.Media.RELATIVE_PATH} = ? AND ${MediaStore.Images.Media.DISPLAY_NAME} = ?",
            arrayOf(folder, name),
            null,
        )?.use { cursor ->
            if (cursor.moveToFirst()) ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, cursor.getLong(0)) else null
        }
    }

    fun view(context: Context, uri: Uri, mime: String = "image/*") = context.startActivity(
        Intent(Intent.ACTION_VIEW).setDataAndType(uri, mime).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
    )

    fun open(context: Context, url: String) = context.startActivity(
        Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    )

    fun sendTo(context: Context, uri: Uri, mime: String = MIME) = context.startActivity(
        Intent.createChooser(
            Intent(Intent.ACTION_SEND).setType(mime).putExtra(Intent.EXTRA_STREAM, uri)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION),
            null,
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    )
}
