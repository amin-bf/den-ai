package den.android

import org.json.JSONArray
import org.json.JSONObject
import java.io.InputStream

/**
 * Android's org.json answers `optString` with the string "null" for a JSON null, unlike the
 * JVM's; every field that may be null is read through these.
 */
fun JSONObject.textOrNull(key: String): String? =
    if (isNull(key)) null else optString(key).takeIf { it.isNotEmpty() }

fun JSONObject.text(key: String, fallback: String = ""): String = textOrNull(key) ?: fallback

/** One tool call the model asked for, once its streamed pieces are put back together. */
data class ToolCall(val id: String, val name: String, val arguments: String)

/** The model's answer to one request: its text, the tools it called and why it stopped. */
class Turn {
    private val text = StringBuilder()
    private val reasoning = StringBuilder()
    private val calls = linkedMapOf<Int, MutableList<String>>()
    private val ids = mutableMapOf<Int, String>()
    private val names = mutableMapOf<Int, String>()
    var finishReason: String? = null
        private set

    val content: String get() = text.toString()

    /** What the model thought before answering, when the template lets it think. */
    val thinking: String get() = reasoning.toString()

    /** One streamed chunk (`data:` payload) of /v1/chat/completions. */
    fun accept(chunk: JSONObject) {
        val choice = chunk.optJSONArray("choices")?.optJSONObject(0) ?: return
        choice.optString("finish_reason").takeIf { it.isNotEmpty() && it != "null" }?.let { finishReason = it }
        val delta = choice.optJSONObject("delta") ?: choice.optJSONObject("message") ?: return
        acceptDelta(delta)
    }

    /** A whole message, as a non-streamed response carries it. */
    fun acceptMessage(message: JSONObject) = acceptDelta(message)

    private fun acceptDelta(delta: JSONObject) {
        delta.opt("content")?.takeIf { it != JSONObject.NULL }?.let { text.append(it.toString()) }
        // llama-server keeps the thinking apart, streamed and whole alike.
        delta.opt("reasoning_content")?.takeIf { it != JSONObject.NULL }?.let { reasoning.append(it.toString()) }
        val toolCalls = delta.optJSONArray("tool_calls") ?: return
        for (i in 0 until toolCalls.length()) {
            val call = toolCalls.getJSONObject(i)
            // A stream numbers the calls with "index"; a whole message just lists them.
            val index = if (call.has("index")) call.getInt("index") else i
            calls.getOrPut(index) { mutableListOf() }
            call.optString("id").takeIf { it.isNotEmpty() }?.let { ids[index] = it }
            val function = call.optJSONObject("function") ?: continue
            function.optString("name").takeIf { it.isNotEmpty() }?.let { names[index] = it }
            function.opt("arguments")?.takeIf { it != JSONObject.NULL }?.let { calls[index]!!.add(it.toString()) }
        }
    }

    fun toolCalls(): List<ToolCall> = calls.entries.map { (index, parts) ->
        ToolCall(ids[index] ?: "call_$index", names[index].orEmpty(), parts.joinToString(""))
    }
}

/** Server-sent events, as /v1/chat/completions streams them. */
object Sse {
    /**
     * Calls [onData] with each event's `data:` payload until `[DONE]` or the end of the stream.
     * The stream is read line by line, so payloads arrive as they are written.
     */
    fun events(input: InputStream, onData: (String) -> Unit) {
        val data = StringBuilder()
        val reader = input.bufferedReader()
        while (true) {
            val line = reader.readLine() ?: break
            when {
                line.isEmpty() -> {
                    if (data.isNotEmpty()) {
                        val payload = data.toString()
                        data.setLength(0)
                        if (payload == "[DONE]") return
                        onData(payload)
                    }
                }
                line.startsWith(":") -> Unit // a comment, e.g. a keep-alive
                line.startsWith("data:") -> {
                    if (data.isNotEmpty()) data.append('\n')
                    data.append(line.removePrefix("data:").removePrefix(" "))
                }
                // other fields (event:, id:, retry:) don't matter here
            }
        }
        if (data.isNotEmpty() && data.toString() != "[DONE]") onData(data.toString())
    }
}

class ToolArgumentException(message: String) : Exception(message)

/**
 * The one tool the chat offers: den's image generation, reduced to what this app supports.
 * The schema and the checks are built from the broker's own `/client` spec, so a workflow or a
 * setting the den doesn't have never reaches it.
 */
object ImageTool {
    const val NAME = "generate_image"
    /** The argument that names a picture of this conversation to edit, by its id. */
    const val WHICH = "image"

    /** Settings no form or tool offers: paths, nested inputs, or set by the app itself. */
    val NOT_FIELDS = setOf(
        "prompt", "workflow", "image", "out", "references", "control", "loras", "upscale",
        "save_maps", "preview", "switch_back", "bytes",
    )

    /** What the tool itself doesn't take (it does take a prompt and a workflow). */
    private val SKIP = NOT_FIELDS - "prompt" - "workflow"

    private fun properties(info: JSONObject): JSONObject? =
        info.optJSONObject("image")?.optJSONObject("parameters")?.optJSONObject("properties")

    fun workflows(info: JSONObject): List<String> =
        info.optJSONObject("image")?.optJSONArray("workflows")
            ?.let { a -> (0 until a.length()).map { a.getString(it) } }.orEmpty()

    /** The workflows of this den that can edit an image. */
    fun edits(info: JSONObject): List<String> =
        info.optJSONObject("image")?.optJSONArray("edits")
            ?.let { a -> (0 until a.length()).map { a.getString(it) } }.orEmpty()

    /**
     * The workflow to edit with: the model's choice when it can edit, otherwise this den's first
     * one that can.
     */
    fun editWorkflow(info: JSONObject, wanted: String?): String {
        val edits = edits(info)
        if (edits.isEmpty()) throw ToolArgumentException("this den has no workflow that can edit an image")
        return wanted?.takeIf { it in edits } ?: edits.first()
    }

    /**
     * The tools array for /v1/chat/completions, or null when this den has no images.
     * [canEditAttached] adds the flag that edits the picture the user attached.
     */
    fun tools(info: JSONObject, ids: List<String> = emptyList()): JSONArray? {
        val props = properties(info) ?: return null
        val image = info.optJSONObject("image") ?: return null
        if (image.opt("unavailable") != null && image.opt("unavailable") != JSONObject.NULL) return null
        val workflows = workflows(info)
        if (workflows.isEmpty()) return null
        val taken = JSONObject()
        for (name in props.keys()) {
            if (name in SKIP) continue
            val p = props.getJSONObject(name)
            if (p.optString("type") !in setOf("string", "integer", "number", "boolean")) continue
            val copy = JSONObject(p.toString())
            if (name == "workflow") {
                copy.put("enum", JSONArray(workflows))
                copy.put("description", "Which workflow to use. Default: ${image.optString("default")}.")
            }
            taken.put(name, copy)
        }
        if (ids.isNotEmpty() && edits(info).isNotEmpty()) {
            taken.put(
                WHICH,
                JSONObject().put("type", "string").put(
                    "description",
                    "To edit a picture of this conversation instead of making a new one: its id, as shown next to " +
                        "it, e.g. \"${ids.last()}\". Several ids separated by commas edit each of them, and \"all\" " +
                        "edits every picture of the user's last message. The prompt is then the instruction, e.g. " +
                        "\"make the sky red, keep the rest\". The pictures here now: ${ids.joinToString(", ")}. " +
                        "Only workflows that can edit take it: ${edits(info).joinToString(", ")}.",
                ),
            )
        }
        val function = JSONObject()
            .put("name", NAME)
            .put(
                "description",
                "Generate an image on the den's GPU and show it to the user in this chat. " +
                    (image.optString("listing").takeIf { it.isNotEmpty() }?.let { "The workflows:\n$it" } ?: ""),
            )
            .put("parameters", JSONObject().put("type", "object").put("properties", taken).put("required", JSONArray(listOf("prompt"))))
        return JSONArray().put(JSONObject().put("type", "function").put("function", function))
    }

    /**
     * Which pictures a call is about, by id: the ones it named, every picture of the user's last
     * message for "all", and none when it asked for no edit at all.
     */
    fun targets(request: JSONObject, lastMessage: List<String>): List<String> {
        val which = request.optString(WHICH).takeIf { it.isNotEmpty() } ?: return emptyList()
        return if (which == "all") lastMessage else which.split(",").map { it.trim() }.filter { it.isNotEmpty() }
    }

    /**
     * The request for POST /image built from the model's arguments, or a [ToolArgumentException]
     * whose message goes back to the model as the tool's result so it can correct itself.
     */
    fun request(info: JSONObject, arguments: String, ids: List<String> = emptyList()): JSONObject {
        val args = try {
            JSONObject(arguments.ifBlank { "{}" })
        } catch (e: Exception) {
            throw ToolArgumentException("the arguments are not valid JSON: ${e.message}")
        }
        val props = properties(info) ?: throw ToolArgumentException("this den has no image generation")
        val request = JSONObject()
        for (name in args.keys()) {
            if (name == WHICH) {
                val wanted = args.get(name).toString().split(",").map { it.trim() }.filter { it.isNotEmpty() }
                val chosen = if (wanted.size == 1 && wanted[0].lowercase() == "all") listOf("all") else wanted
                val unknown = chosen.filter { it != "all" && it !in ids }
                if (chosen.isEmpty() || unknown.isNotEmpty()) {
                    throw ToolArgumentException(
                        "no picture ${unknown.joinToString(", ")} in this conversation; the pictures here are: " +
                            (ids.joinToString(", ").ifEmpty { "none yet" }) + " (or \"all\" for the last message's)"
                    )
                }
                request.put(WHICH, chosen.joinToString(","))
                continue
            }
            if (name in SKIP || props.optJSONObject(name) == null) {
                val allowed = props.keys().asSequence().filter { it !in SKIP }.sorted().joinToString(", ")
                throw ToolArgumentException("unknown setting $name; this den takes: $allowed")
            }
            val spec = props.getJSONObject(name)
            val value = args.get(name)
            // The workflows this den runs now, not what the schema's enum happened to list.
            val enum = if (name == "workflow") workflows(info)
            else spec.optJSONArray("enum")?.let { a -> (0 until a.length()).map { a.get(it).toString() } }
            val text = value.toString()
            if (enum != null && text !in enum) {
                throw ToolArgumentException("$name must be one of: ${enum.joinToString(", ")} (got $text)")
            }
            when (spec.optString("type")) {
                "integer" -> request.put(name, text.toLongOrNull() ?: throw ToolArgumentException("$name must be a whole number (got $text)"))
                "number" -> request.put(name, text.toDoubleOrNull() ?: throw ToolArgumentException("$name must be a number (got $text)"))
                "boolean" -> request.put(name, text.toBooleanStrictOrNull() ?: throw ToolArgumentException("$name must be true or false (got $text)"))
                else -> request.put(name, text)
            }
        }
        val prompt = request.optString("prompt")
        if (prompt.isBlank()) throw ToolArgumentException("prompt is required")
        request.optString("size").takeIf { it.isNotEmpty() }?.let { size ->
            if (!Regex("^\\d{2,5}x\\d{2,5}$").matches(size)) {
                throw ToolArgumentException("size must be WIDTHxHEIGHT, e.g. 1024x1024 (got $size)")
            }
        }
        return request
    }
}

/**
 * Taking the end of a conversation back: another go at the same question (retry), or the last
 * message returned to the input box to be changed and sent again (resend). Both only ever cut
 * at the end, and a discarded turn's tool calls and results go with it so the model never sees
 * them again. Images those calls made stay on disk; only their bubbles go.
 */
object History {
    fun canRetry(messages: List<ChatMessage>): Boolean =
        messages.any { it.role == "user" } && messages.lastOrNull()?.role != "user"

    fun canResend(messages: List<ChatMessage>): Boolean = messages.any { it.role == "user" }

    /** Everything up to and including the last message the user sent. */
    fun withoutLastAnswer(messages: List<ChatMessage>): List<ChatMessage> {
        val lastSent = messages.indexOfLast { it.role == "user" }
        return if (lastSent < 0) emptyList() else messages.take(lastSent + 1)
    }

    /** Everything before the last message the user sent, and that message itself. */
    fun withoutLastSent(messages: List<ChatMessage>): Pair<List<ChatMessage>, ChatMessage?> {
        val lastSent = messages.indexOfLast { it.role == "user" }
        if (lastSent < 0) return messages.toList() to null
        return messages.take(lastSent) to messages[lastSent]
    }
}

/**
 * What a chat turn asks for, built where it can be checked without a device. The pictures a
 * message carries are passed in as data URLs, because reading and encoding them needs Android.
 */
object ChatRequest {
    fun build(
        model: String,
        numCtx: Int,
        systemPrompt: String,
        history: List<ChatMessage>,
        pictures: (ChatMessage) -> List<Pair<String, String>>,
        tools: JSONArray? = null,
        stream: Boolean = true,
        thinking: Boolean = false,
    ): JSONObject {
        val system = JSONObject().put("role", "system").put("content", systemPrompt)
        // An assistant message with nothing in it is a turn that failed; it says nothing here.
        val said = history.filter { it.role != "assistant" || it.content.isNotBlank() || it.toolCalls != null }
        val messages = Context.fit(listOf(system) + said.map { withPictures(it, pictures) }, numCtx)
        val body = JSONObject()
            .put("model", model)
            .put("messages", JSONArray(messages))
            .put("stream", stream)
            .put("chat_template_kwargs", JSONObject().put("enable_thinking", thinking))
        tools?.let { body.put("tools", it).put("tool_choice", "auto") }
        return body
    }

    /**
     * A message as the model takes it. What the user attached goes along as OpenAI-style image
     * parts; what den generated does not, the tool's result already says what it made.
     */
    private fun withPictures(message: ChatMessage, pictures: (ChatMessage) -> List<Pair<String, String>>): JSONObject {
        val api = message.toApi()
        if (message.role != "user" || message.pictures.isEmpty()) return api
        val urls = pictures(message)
        if (urls.isEmpty()) return api
        val parts = JSONArray()
        if (message.content.isNotBlank()) parts.put(JSONObject().put("type", "text").put("text", message.content))
        urls.forEach { (id, url) ->
            // Each picture says which one it is, and nothing else: an id holds still, a count
            // ("the second one") means something different in every message.
            parts.put(JSONObject().put("type", "text").put("text", "[$id]"))
            parts.put(JSONObject().put("type", "image_url").put("image_url", JSONObject().put("url", url)))
        }
        return api.put("content", parts)
    }

    /** Whether a request carries a picture, which changes what a failure is likely to mean. */
    fun carriesPictures(body: JSONObject): Boolean {
        val messages = body.optJSONArray("messages") ?: return false
        return (0 until messages.length()).any { messages.getJSONObject(it).opt("content") is JSONArray }
    }
}

/** What went wrong, said plainly, with the den's own words kept. */
object Failures {
    private val VISION = listOf("mmproj", "multimodal", "vision", "image input", "projector", "not support image")
    private val MODEL = listOf("model", "not found", "pull", "no such")
    private val CONNECTION = listOf("unreachable", "connection is closed", "connection reset", "timed out", "timeout", "broken pipe", "refused a channel")

    /** Only what the server itself says about images counts as "this model cannot see". */
    fun aboutVision(text: String?): Boolean =
        text?.lowercase()?.let { message -> VISION.any { it in message } } == true

    /**
     * The line the chat shows. A failure that says nothing about images is not diagnosed as one:
     * a timeout or a broken connection gets its own words back.
     */
    /** A failure of the connection itself, not an answer from the den. */
    fun aboutConnection(text: String?): Boolean =
        text?.lowercase()?.let { message -> CONNECTION.any { it in message } } == true

    /** A failure about the model this conversation asked for, rather than the den itself. */
    fun aboutModel(text: String?): Boolean =
        text?.lowercase()?.let { message -> MODEL.count { it in message } >= 2 } == true

    fun chat(text: String?, carriedPictures: Boolean): String {
        val said = text?.takeIf { it.isNotBlank() } ?: "no reason given"
        return if (aboutConnection(said)) {
            "the connection to den dropped: $said — the next message reconnects by itself, or " +
                "use Connect to set it up again"
        } else if (carriedPictures && aboutVision(said)) {
            "this model has no vision, or den didn't load its projector ([llm] vision in its " +
                "config.toml) — send the message without the picture, or pick a model that can " +
                "see. The den said: $said"
        } else {
            "the den could not answer: $said"
        }
    }
}

/** The messages of a chat, trimmed to what the den's context window can hold. */
object Context {
    /** What one attached picture costs the model, near enough for trimming. */
    const val IMAGE_TOKENS = 1200

    /**
     * Rough, deliberately generous: llama.cpp counts tokens, we only need to stay under. An
     * attached image counts as a picture, not as the length of its base64.
     */
    fun tokens(message: JSONObject): Int {
        val content = message.opt("content")
        if (content is JSONArray) {
            var total = 8
            for (i in 0 until content.length()) {
                val part = content.getJSONObject(i)
                total += if (part.optString("type") == "image_url") IMAGE_TOKENS else part.optString("text").length / 3
            }
            return total
        }
        return (message.toString().length / 3) + 8
    }

    /**
     * Keeps the system message and as many of the newest messages as fit in [numCtx], leaving
     * room for the answer. A tool result never stays without the call it answers.
     */
    fun fit(messages: List<JSONObject>, numCtx: Int): List<JSONObject> {
        val budget = (numCtx.takeIf { it > 0 } ?: 8192) / 2
        val system = messages.firstOrNull()?.takeIf { it.optString("role") == "system" }
        val rest = if (system != null) messages.drop(1) else messages
        var used = system?.let { tokens(it) } ?: 0
        val kept = ArrayDeque<JSONObject>()
        for (message in rest.asReversed()) {
            val cost = tokens(message)
            if (used + cost > budget && kept.isNotEmpty()) break
            kept.addFirst(message)
            used += cost
        }
        // A tool result whose assistant call was dropped confuses the template; drop it too.
        while (kept.isNotEmpty() && kept.first().optString("role") == "tool") kept.removeFirst()
        return listOfNotNull(system) + kept
    }
}
