package den.android

import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.Closeable
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

class DenException(message: String) : Exception(message)

/** Thrown when the user stopped a request; what arrived before it still stands. */
class StoppedException : Exception("stopped")

/**
 * The user's Stop for one turn. It closes the response being read, and with it the channel: the
 * broker takes that for a caller who hung up and stops the model, or cancels the image. A
 * response that opens after it (the answer only starts once a model has loaded) is closed as
 * soon as it arrives.
 */
class Stop {
    @Volatile var stopped = false
        private set
    private var reading: Closeable? = null

    fun stop() {
        val open = synchronized(this) {
            stopped = true
            reading
        }
        runCatching { open?.close() }
    }

    /** Throws [StoppedException] once the user has stopped. */
    fun check() {
        if (stopped) throw StoppedException()
    }

    internal fun watch(resp: Closeable?) {
        val closeNow = synchronized(this) {
            reading = resp
            stopped && resp != null
        }
        if (closeNow) runCatching { resp?.close() }
    }
}

/**
 * The broker's HTTP API as a client on another machine uses it (ADR 0007): only the routes
 * below, because the broker hands any path it doesn't know to the LLM, which loads a model.
 * No path of either machine crosses, only bytes: files go as their name and contents, input
 * images as their name and base64, and images come back as base64 ("bytes": true).
 */
class DenClient(private val transport: Transport) {
    constructor(baseUrl: String) : this(UrlTransport(baseUrl))

    private var checked = false

    fun status(): JSONObject = request("GET", "/status", null, 15_000)

    /** Mode, model, enabled tasks and the image spec; asks /status first, like den/remote.py. */
    fun client(): JSONObject {
        if (!checked) {
            // An older broker would take /client for an LLM request and load a model for it;
            // its /status has no llm_model.
            if (!status().has("llm_model")) {
                throw DenException("den: this broker predates remote clients; update den there and restart its broker")
            }
            checked = true
        }
        return request("GET", "/client", null, 30_000)
    }

    /** files: (file name, contents) pairs read on this device. */
    fun delegate(task: String, instructions: String, text: String?, files: List<Pair<String, String>>): JSONObject {
        val body = JSONObject()
            .put("task", task)
            .put("instructions", instructions)
            .put("text", text ?: JSONObject.NULL)
            .put("files", JSONArray(files.map { (name, content) -> JSONObject().put("path", name).put("content", content) }))
        // The answer comes once the model is done, which can take minutes after a load.
        return request("POST", "/delegate", body, (900 + 120) * 1000)
    }

    fun feedback(id: Any, verdict: String, note: String): JSONObject =
        request("POST", "/feedback", JSONObject().put("id", id).put("verdict", verdict).put("note", note), 15_000)

    /**
     * den's own agent skills on the broker's machine. Only what a user picks is ever loaded, so
     * this is asked for when the picker opens, not on connecting.
     */
    fun skills(): JSONArray = request("GET", "/skills", null, 30_000).optJSONArray("skills") ?: JSONArray()

    /** One skill's text, or one of its reference files. */
    fun skill(name: String, reference: String? = null): JSONObject {
        val query = "name=" + URLEncoder.encode(name, "UTF-8") +
            (reference?.let { "&reference=" + URLEncoder.encode(it, "UTF-8") } ?: "")
        return request("GET", "/skill?$query", null, 30_000)
    }

    /**
     * What models the broker's machine has, from Ollama's catalogue through the broker. It
     * loads nothing; the den's own selected model is in /status.
     */
    fun models(): List<String> {
        val data = request("GET", "/v1/models", null, 30_000).optJSONArray("data") ?: return emptyList()
        return (0 until data.length()).mapNotNull { data.getJSONObject(it).textOrNull("id") }
    }

    private var clipsChecked = false

    /** An older broker would take /clip for an LLM request and load a model for it (ADR 0009). */
    private fun requireClips() {
        if (!clipsChecked) {
            if (!status().has("clips")) {
                throw DenException("den: this broker predates clips; update den there and restart its broker")
            }
            clipsChecked = true
        }
    }

    /** POST /clip: starts a clip, a detached request, and answers {id, estimate_s, summary} at once. */
    fun startClip(body: JSONObject): JSONObject {
        requireClips()
        return request("POST", "/clip", body, 300_000)
    }

    /** GET /clip?id=N: how the clip goes; once done, with [bytes], the clip and its contact sheet. */
    fun clip(id: Int, bytes: Boolean = false): JSONObject {
        requireClips()
        return request("GET", "/clip?id=$id" + if (bytes) "&bytes=1" else "", null, 300_000)
    }

    /** POST /voices {name, recording: {name, base64}, replace}: keep a recording as a voice (ADR 0010). */
    fun addVoice(body: JSONObject): JSONObject = request("POST", "/voices", body, 120_000)

    fun cancelClip(id: Int): JSONObject {
        requireClips()
        return request("POST", "/clip/cancel", JSONObject().put("id", id), 30_000)
    }

    fun poses(): JSONObject = request("GET", "/poses", null, 30_000)

    /** POST /poses {name, remove}: delete a saved pose, when the user asks. */
    fun removePose(name: String): JSONObject =
        request("POST", "/poses", JSONObject().put("name", name).put("remove", true), 30_000)

    /** POST /voices {name, remove}: delete a voice, when the user asks. */
    fun removeVoice(name: String): JSONObject =
        request("POST", "/voices", JSONObject().put("name", name).put("remove", true), 30_000)

    fun pose(name: String): JSONObject =
        request("GET", "/poses?name=" + URLEncoder.encode(name, "UTF-8"), null, 30_000)

    /**
     * POST /v1/chat/completions. With `"stream": true` it reads the SSE events as they arrive and
     * calls [onChunk] for each one, so tokens show while the model writes them; otherwise it
     * reads the one whole message. The answer's text and tool calls come back as a [Turn].
     */
    fun chat(body: JSONObject, stop: Stop? = null, onChunk: (JSONObject) -> Unit = {}): Turn {
        val turn = Turn()
        if (!body.optBoolean("stream")) {
            val answer = request("POST", "/v1/chat/completions", body, CHAT_TIMEOUT_MS, stop)
            val choice = answer.optJSONArray("choices")?.optJSONObject(0)
                ?: throw DenException("den: the model returned no message")
            // The reason first (accept stops at a chunk without a delta), then the message.
            turn.accept(JSONObject().put("choices", JSONArray().put(JSONObject().put("finish_reason", choice.optString("finish_reason")))))
            turn.acceptMessage(choice.optJSONObject("message") ?: throw DenException("den: the model returned no message"))
            return turn
        }
        exchange("POST", "/v1/chat/completions", body, CHAT_TIMEOUT_MS).use { resp ->
            reading(resp, stop) {
                Sse.events(resp.body) { payload ->
                    val chunk = JSONObject(payload)
                    if (chunk.has("error")) throw DenException(errorText(chunk))
                    turn.accept(chunk)
                    onChunk(chunk)
                }
            }
        }
        return turn
    }

    /**
     * POST /image with "bytes": true. Calls [onProgress] for each NDJSON progress line and
     * returns the final "result"; an {"error"} line or an HTTP error throws [DenException].
     */
    fun image(request: JSONObject, stop: Stop? = null, onProgress: (JSONObject) -> Unit): JSONObject =
        streamed("/image", request, stop, onProgress)

    /**
     * POST /pose with "bytes": true, read like [image]. With "draw_only" the broker draws the
     * skeleton and saves nothing, so it can be shown before it is kept; the same request with a
     * name and a description, drawn a second time, is what saves it in the den's pose library.
     */
    fun pose(request: JSONObject, onProgress: (JSONObject) -> Unit): JSONObject =
        streamed("/pose", request, null, onProgress)

    /** POST /voice with bytes: speech in a voice, the track back as base64 "audio" (ADR 0010). */
    fun speak(request: JSONObject, onProgress: (JSONObject) -> Unit): JSONObject =
        streamed("/voice", request, null, onProgress)

    /** POST /voices {draft, name}: keep a designed draft voice under name. */
    fun saveDraft(draft: String, name: String): JSONObject =
        request("POST", "/voices", JSONObject().put("draft", draft).put("name", name).put("replace", true), 30_000)

    /** GET /voices?name=N&bytes=1: one voice's details and its sample, to listen to it. */
    fun voice(name: String): JSONObject =
        request("GET", "/voices?name=" + java.net.URLEncoder.encode(name, "UTF-8") + "&bytes=1", null, 60_000)

    /** POST /voices/design {name, description, language?}: a voice made from a description (ADR 0010). */
    fun designVoice(request: JSONObject, onProgress: (JSONObject) -> Unit): JSONObject =
        streamed("/voices/design", request, null, onProgress)

    /** POST /transcribe {audio: {name, base64}}: what a recording says, as a timed SRT (ADR 0010). */
    fun transcribe(request: JSONObject, onProgress: (JSONObject) -> Unit): JSONObject =
        streamed("/transcribe", request, null, onProgress)

    /** An endpoint that streams NDJSON progress lines and ends with {"result"} or {"error"}. */
    private fun streamed(path: String, request: JSONObject, stop: Stop?, onProgress: (JSONObject) -> Unit): JSONObject {
        request.put("bytes", true)
        exchange("POST", path, request, 30 * 60 * 1000).use { resp ->
            reading(resp, stop) {
                resp.body.bufferedReader().useLines { lines ->
                    for (line in lines) {
                        if (line.isBlank()) continue
                        val msg = JSONObject(line)
                        when {
                            msg.has("result") -> return msg.getJSONObject("result")
                            msg.has("error") -> throw DenException(errorText(msg))
                            else -> onProgress(msg)
                        }
                    }
                }
                throw DenException("den: the broker closed the stream without a result")
            }
        }
    }

    private fun request(method: String, path: String, body: JSONObject?, timeoutMs: Int, stop: Stop? = null): JSONObject =
        exchange(method, path, body, timeoutMs).use { resp ->
            reading(resp, stop) { JSONObject(resp.body.bufferedReader().readText()) }
        }

    /**
     * Reads [resp], which [stop] may close meanwhile: whatever the read then ends with, a closed
     * stream, an error or a half line, the user's Stop is the reason.
     */
    private inline fun <T> reading(resp: Response, stop: Stop?, block: () -> T): T {
        stop?.watch(resp)
        try {
            return block().also { stop?.check() }
        } catch (e: Exception) {
            stop?.check()
            throw if (e is IOException) unreachable(e) else e
        } finally {
            stop?.watch(null)
        }
    }

    /** A 2xx response to read; anything else becomes a DenException with the broker's message. */
    private fun exchange(method: String, path: String, body: JSONObject?, timeoutMs: Int): Response {
        val headers = mutableMapOf("X-Den-Caller" to CALLER, "Accept" to "application/json")
        if (body != null) headers["Content-Type"] = "application/json"
        val resp = try {
            transport.open(method, path, headers, body?.toString()?.toByteArray(), timeoutMs)
        } catch (e: IOException) {
            throw unreachable(e)
        }
        if (resp.status in 200..299) return resp
        resp.use {
            val text = runCatching { it.body.bufferedReader().readText() }.getOrDefault("")
            val parsed = runCatching { errorText(JSONObject(text)) }.getOrNull()
            throw DenException(parsed ?: "den: HTTP ${resp.status} ${text.take(200)}")
        }
    }

    private fun unreachable(e: IOException): DenException =
        DenException("den: broker unreachable ${transport.label} (${e.message ?: e.javaClass.simpleName})")

    companion object {
        const val CALLER = "android"

        /** A turn can wait for the model to load and then write for a while. */
        const val CHAT_TIMEOUT_MS = 20 * 60 * 1000

        /** {"error": "den: …"} or OpenAI-style {"error": {"message": …}}. */
        fun errorText(msg: JSONObject): String {
            val error = msg.opt("error")
            return if (error is JSONObject) error.optString("message", error.toString()) else error.toString()
        }

        /** One progress line of /image in words, e.g. "generating: z-image-turbo · seed 1 · 1024x1024". */
        fun describeProgress(msg: JSONObject): String {
            val key = msg.keys().asSequence().firstOrNull() ?: return ""
            val value = msg.opt(key)
            return when {
                key == "generating" && value is JSONObject -> {
                    val summary = value.optJSONArray("summary")
                    "generating: " + if (summary != null) (0 until summary.length()).joinToString(" · ") { summary.getString(it) } else value.toString()
                }
                key == "waiting" && value is JSONObject -> "waiting: " + value.optString("reason", value.toString())
                key == "drawing" -> "drawing the pose"
                value is JSONArray -> "$key: " + (0 until value.length()).joinToString(", ") { value.get(it).toString() }
                else -> "$key: $value"
            }
        }
    }
}

/** Plain HTTP with HttpURLConnection: the direct URL, for development only. */
class UrlTransport(baseUrl: String) : Transport {
    private val base = baseUrl.trimEnd('/')
    override val label = "at $base"

    override fun open(method: String, path: String, headers: Map<String, String>, body: ByteArray?, timeoutMs: Int): Response {
        val conn = URL(base + path).openConnection() as HttpURLConnection
        try {
            conn.requestMethod = method
            conn.connectTimeout = 10_000
            conn.readTimeout = timeoutMs
            headers.forEach { (k, v) -> conn.setRequestProperty(k, v) }
            if (body != null) {
                conn.doOutput = true
                conn.setFixedLengthStreamingMode(body.size)
                conn.outputStream.use { it.write(body) }
            }
            val status = conn.responseCode
            val stream = (if (status in 200..299) conn.inputStream else conn.errorStream) ?: ByteArrayInputStream(ByteArray(0))
            return Response(status, stream) { conn.disconnect() }
        } catch (e: IOException) {
            conn.disconnect()
            throw e
        }
    }
}
