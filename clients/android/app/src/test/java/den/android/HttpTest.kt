package den.android

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.SequenceInputStream
import java.util.Collections

/** Serves one canned raw HTTP response per request and keeps what was sent, like a channel. */
private class RawTransport(private vararg val responses: () -> InputStream) : Transport {
    override val label = "(test)"
    val sent = mutableListOf<String>()
    private var next = 0

    override fun open(method: String, path: String, headers: Map<String, String>, body: ByteArray?, timeoutMs: Int): Response {
        val out = ByteArrayOutputStream()
        Http.writeRequest(out, method, path, "127.0.0.1:11435", headers, body)
        sent += out.toString("ISO-8859-1")
        return Http.readResponse(responses[next++]())
    }
}

/** Hands out its bytes in the given pieces, one piece per read at most, as a network would. */
private fun pieces(vararg parts: String): InputStream =
    SequenceInputStream(Collections.enumeration(parts.map { ByteArrayInputStream(it.toByteArray()) }))

private fun chunk(data: String) = "${Integer.toHexString(data.toByteArray().size)}\r\n$data\r\n"

class HttpTest {
    @Test
    fun requestHasTheHeadersAndBody() {
        val out = ByteArrayOutputStream()
        Http.writeRequest(out, "POST", "/feedback", "127.0.0.1:11435", mapOf("X-Den-Caller" to "android"), "{}".toByteArray())
        assertEquals(
            "POST /feedback HTTP/1.1\r\nHost: 127.0.0.1:11435\r\nX-Den-Caller: android\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
            out.toString("ISO-8859-1"),
        )
    }

    @Test
    fun contentLengthResponse() {
        val body = """{"mode": "on", "llm_model": "m"}"""
        val t = RawTransport({ pieces("HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${body.length}\r\n\r\n", body, "trailing junk") })
        val status = DenClient(t).status()
        assertEquals("m", status.getString("llm_model"))
        assertTrue(t.sent[0].startsWith("GET /status HTTP/1.1\r\n"))
        assertTrue(t.sent[0].contains("X-Den-Caller: android\r\n"))
        assertTrue(t.sent[0].contains("Connection: close\r\n"))
    }

    @Test
    fun chunkedNdjsonSplitAcrossChunks() {
        val lines = listOf(
            """{"waiting": {"reason": "busy"}}""",
            """{"generating": {"summary": ["w", "seed 1"]}}""",
            """{"result": {"seed": 1, "images": ["AAAA"]}}""",
        ).joinToString("\n") + "\n"
        // Chunks cut through the middle of lines, and each chunk arrives in pieces.
        val cuts = listOf(0, 10, 45, 46, 90, lines.length)
        val body = cuts.zipWithNext { a, b -> chunk(lines.substring(a, b)) }.joinToString("") + "0\r\n\r\n"
        val head = "HTTP/1.0 200 OK\r\nContent-Type: application/x-ndjson\r\nTransfer-Encoding: chunked\r\n\r\n"
        val t = RawTransport({ pieces(head, *body.chunked(7).toTypedArray()) })
        val progress = mutableListOf<String>()
        val request = JSONObject().put("prompt", "p")
        val result = DenClient(t).image(request) { progress += DenClient.describeProgress(it) }
        assertEquals(listOf("waiting: busy", "generating: w · seed 1"), progress)
        assertEquals(1, result.getInt("seed"))
        assertTrue("sends bytes: true", t.sent[0].contains("\"bytes\":true"))
    }

    @Test
    fun errorBeforeTheStreamIs503Json() {
        val body = """{"error": "den: no workflow"}"""
        val t = RawTransport({ pieces("HTTP/1.0 503 Service Unavailable\r\nContent-Length: ${body.length}\r\n\r\n$body") })
        try {
            DenClient(t).image(JSONObject().put("prompt", "p")) {}
            fail("expected a DenException")
        } catch (e: DenException) {
            assertEquals("den: no workflow", e.message)
        }
    }

    @Test
    fun errorLineInsideTheStream() {
        val body = chunk("""{"starting": "comfyui"}""" + "\n") + chunk("""{"error": "den: out of memory"}""" + "\n") + "0\r\n\r\n"
        val t = RawTransport({ pieces("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n", body) })
        val progress = mutableListOf<String>()
        try {
            DenClient(t).image(JSONObject().put("prompt", "p")) { progress += DenClient.describeProgress(it) }
            fail("expected a DenException")
        } catch (e: DenException) {
            assertEquals("den: out of memory", e.message)
        }
        assertEquals(listOf("starting: comfyui"), progress)
    }

    @Test
    fun streamCutShortIsAnError() {
        val t = RawTransport({ pieces("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n", chunk("""{"starting": "x"}""" + "\n")) })
        try {
            DenClient(t).image(JSONObject().put("prompt", "p")) {}
            fail("expected a DenException")
        } catch (e: DenException) {
            assertTrue(e.message!!.startsWith("den: broker unreachable"))
        }
    }
}
