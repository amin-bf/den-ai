package den.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class ClipMomentTest {
    @Test
    fun startAndEmptyAreTheFirstFrame() {
        assertEquals(0, ClipMoment.parse("start"))
        assertEquals(0, ClipMoment.parse(" "))
    }

    @Test
    fun endStaysAWord() {
        assertEquals("end", ClipMoment.parse("End"))
    }

    @Test
    fun aPercentageStaysAPercentage() {
        assertEquals("50%", ClipMoment.parse("50%"))
        assertEquals("33.5%", ClipMoment.parse(" 33.5 % "))
    }

    @Test
    fun aNumberIsSeconds() {
        assertEquals(2.5, ClipMoment.parse("2.5"))
        assertEquals(3.0, ClipMoment.parse("3s"))
    }

    @Test
    fun anythingElseIsRefusedWithWhatWouldDo() {
        val e = assertThrows(DenException::class.java) { ClipMoment.parse("halfway") }
        assertEquals(true, e.message!!.contains("50%"))
    }
}
