package den.android

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import androidx.exifinterface.media.ExifInterface
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File

/**
 * Bitmaps are real only on a device, so what a camera's orientation tag does to a picture is
 * checked here: a photo held upright must arrive upright, with its corners where they belong.
 */
@RunWith(AndroidJUnit4::class)
class MediaExifTest {
    private val context = InstrumentationRegistry.getInstrumentation().targetContext

    /** A landscape JPEG, white but for a red square in its top-left corner, tagged [orientation]. */
    private fun jpeg(width: Int, height: Int, orientation: Int): ByteArray {
        val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        bitmap.eraseColor(Color.WHITE)
        for (x in 0 until width / 4) for (y in 0 until height / 4) bitmap.setPixel(x, y, Color.RED)
        val out = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.JPEG, 95, out)
        val file = File(context.cacheDir, "exif-test-$orientation.jpg")
        file.writeBytes(out.toByteArray())
        ExifInterface(file.path).apply {
            setAttribute(ExifInterface.TAG_ORIENTATION, orientation.toString())
            saveAttributes()
        }
        return file.readBytes()
    }

    private fun corners(jpeg: ByteArray): Map<String, Int> {
        val bitmap = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size)
        val right = bitmap.width - 1
        val bottom = bitmap.height - 1
        return mapOf(
            "topLeft" to bitmap.getPixel(2, 2),
            "topRight" to bitmap.getPixel(right - 2, 2),
            "bottomLeft" to bitmap.getPixel(2, bottom - 2),
            "bottomRight" to bitmap.getPixel(right - 2, bottom - 2),
        )
    }

    private fun assertRedIsAt(corner: String, jpeg: ByteArray) {
        corners(jpeg).forEach { (where, colour) ->
            val red = Color.red(colour) > 150 && Color.green(colour) < 120 && Color.blue(colour) < 120
            assertEquals("$where should${if (where == corner) "" else " not"} be red (got ${Integer.toHexString(colour)})", where == corner, red)
        }
    }

    @Test
    fun aPictureWithoutAnOrientationTagIsLeftAlone() {
        val attached = Media.scale(jpeg(400, 200, ExifInterface.ORIENTATION_NORMAL), "photo.jpg")!!
        assertEquals(400 to 200, attached.width to attached.height)
        assertRedIsAt("topLeft", attached.jpeg)
    }

    @Test
    fun aQuarterTurnFromTheCameraIsApplied() {
        // Orientation 6: the camera was held so the picture has to be turned 90° clockwise.
        val attached = Media.scale(jpeg(400, 200, ExifInterface.ORIENTATION_ROTATE_90), "photo.jpg")!!
        assertEquals("the picture stands up", 200 to 400, attached.width to attached.height)
        assertEquals("and its size is reported upright", 200 to 400, attached.fromWidth to attached.fromHeight)
        assertRedIsAt("topRight", attached.jpeg)
    }

    @Test
    fun aHalfTurnIsApplied() {
        val attached = Media.scale(jpeg(400, 200, ExifInterface.ORIENTATION_ROTATE_180), "photo.jpg")!!
        assertEquals(400 to 200, attached.width to attached.height)
        assertRedIsAt("bottomRight", attached.jpeg)
    }

    @Test
    fun theResultCarriesNoOrientationOfItsOwn() {
        val attached = Media.scale(jpeg(400, 200, ExifInterface.ORIENTATION_ROTATE_90), "photo.jpg")!!
        val tag = ExifInterface(ByteArrayInputStream(attached.jpeg))
            .getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)
        // Re-encoded from pixels, so there is no tag at all (undefined) — nothing left to apply.
        assertTrue("no turn is left to apply, got $tag", tag == ExifInterface.ORIENTATION_UNDEFINED || tag == ExifInterface.ORIENTATION_NORMAL)
        assertEquals(Media.Upright(0, false), Media.upright(tag))
    }

    @Test
    fun aBigPhotoIsScaledAndStillUpright() {
        val attached = Media.scale(jpeg(3000, 2000, ExifInterface.ORIENTATION_ROTATE_90), "photo.jpg")!!
        assertEquals(2000 to 3000, attached.fromWidth to attached.fromHeight)
        assertTrue("long side is at most ${Media.LONG_SIDE}", maxOf(attached.width, attached.height) <= Media.LONG_SIDE)
        assertTrue("portrait after the turn", attached.height > attached.width)
        assertRedIsAt("topRight", attached.jpeg)
    }
}

/** Several pictures attached to one message must stay several pictures. */
@RunWith(AndroidJUnit4::class)
class AttachmentsTest {
    private val context = InstrumentationRegistry.getInstrumentation().targetContext

    private fun picture(width: Int, height: Int): Attached {
        val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        bitmap.eraseColor(Color.rgb(width % 256, height % 256, 128))
        val out = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.JPEG, 90, out)
        return Media.scale(out.toByteArray(), "photo.jpg")!!
    }

    @Test
    fun threeAttachmentsSavedTogetherKeepTheirOwnFiles() {
        val store = ChatStore(context)
        val conversation = store.create()
        try {
            val pictures = listOf(picture(300, 200), picture(400, 200), picture(500, 200))
            val saved = pictures.map { store.saveAttachment(conversation, it) }
            assertEquals("each one has its own file", 3, saved.map { it.file }.toSet().size)
            assertEquals("and its own id", listOf("img-1", "img-2", "img-3"), saved.map { it.id })
            saved.forEachIndexed { i, picture ->
                val kept = store.bytesOf(conversation, picture.file)!!
                assertTrue("picture $i is itself, not the last one", kept.contentEquals(pictures[i].jpeg))
            }
        } finally {
            store.delete(conversation, withImages = false)
        }
    }
}
