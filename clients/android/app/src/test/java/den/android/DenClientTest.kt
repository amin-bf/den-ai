package den.android

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

class DenClientTest {
    private fun describe(line: String) = DenClient.describeProgress(JSONObject(line))

    @Test
    fun progressLinesInWords() {
        assertEquals(
            "generating: z-image-turbo · seed 7 · 1024x1024",
            describe("""{"generating": {"workflow": "z-image-turbo", "summary": ["z-image-turbo", "seed 7", "1024x1024"]}}"""),
        )
        assertEquals("waiting: busy", describe("""{"waiting": {"reason": "busy", "load": 3}}"""))
        assertEquals("unloading: qwen", describe("""{"unloading": ["qwen"]}"""))
        assertEquals("starting: comfyui", describe("""{"starting": "comfyui"}"""))
    }

    @Test
    fun errorsInBothShapes() {
        assertEquals("den: down", DenClient.errorText(JSONObject("""{"error": "den: down"}""")))
        assertEquals("den: down", DenClient.errorText(JSONObject("""{"error": {"message": "den: down"}}""")))
    }
}
