package den.android

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.SequenceInputStream
import java.util.Collections

/** Serves canned raw HTTP bytes and records what was sent, like the SSH channel does. */
private class CannedTransport(private val raw: String) : Transport {
    override val label = "(test)"
    var sent = ""

    override fun open(method: String, path: String, headers: Map<String, String>, body: ByteArray?, timeoutMs: Int): Response {
        val out = ByteArrayOutputStream()
        Http.writeRequest(out, method, path, "127.0.0.1:11435", headers, body)
        sent = out.toString("ISO-8859-1")
        // One byte at a time, so nothing may depend on where the pieces fall.
        val pieces = raw.map { ByteArrayInputStream(byteArrayOf(it.code.toByte())) as InputStream }
        return Http.readResponse(SequenceInputStream(Collections.enumeration(pieces)))
    }
}

/**
 * Serves [raw] and then hangs, like a model thinking forever, until the response is closed; the
 * read then fails, as the SSH channel's does.
 */
private class HangingTransport(private val raw: String) : Transport {
    override val label = "(test)"
    val closed = java.util.concurrent.CountDownLatch(1)

    override fun open(method: String, path: String, headers: Map<String, String>, body: ByteArray?, timeoutMs: Int): Response {
        val served = ByteArrayInputStream(raw.toByteArray())
        val hang = object : InputStream() {
            override fun read(): Int {
                if (!closed.await(5, java.util.concurrent.TimeUnit.SECONDS)) fail("the response was never closed")
                throw java.io.IOException("channel closed")
            }
        }
        return Http.readResponse(SequenceInputStream(served, hang)) { closed.countDown() }
    }
}

private fun chunk(data: String) ="${Integer.toHexString(data.toByteArray().size)}\r\n$data\r\n"

private fun sseResponse(vararg chunks: String) =
    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n" +
        chunks.joinToString("") { chunk(it) } + "0\r\n\r\n"

private fun delta(content: String) =
    """{"choices": [{"index": 0, "delta": {"content": ${JSONObject.quote(content)}}}]}"""

/** The /client spec of a den with two workflows, cut down to what the tool needs. */
private fun spec() = JSONObject(
    """
    {"mode": "on", "llm": {"model": "m", "unavailable": null},
     "tasks": {},
     "image": {"image_on": true, "unavailable": null, "default": "fast", "workflows": ["fast", "edit"],
               "edits": ["edit"], "listing": "* fast",
               "parameters": {"type": "object", "properties": {
                 "prompt": {"type": "string"},
                 "workflow": {"type": "string", "enum": ["stale"]},
                 "size": {"type": "string"},
                 "seed": {"type": "integer"},
                 "steps": {"type": "integer"},
                 "cfg": {"type": "number"},
                 "sampler": {"type": "string", "enum": ["euler", "dpmpp_2m"]},
                 "image": {"type": "string"},
                 "out": {"type": "string"},
                 "references": {"type": "array", "items": {"type": "string"}}
               }}}}
    """.trimIndent(),
)

class ChatTest {
    // --- streaming ---

    @Test
    fun streamedAnswerArrivesPieceByPiece() {
        // The second event's "data:" line is split across two HTTP chunks.
        val raw = sseResponse(
            "data: ${delta("Hel")}\n\n",
            "data: ${delta("lo the")}".substring(0, 20),
            "${"data: ${delta("lo the")}".substring(20)}\n\n",
            "data: ${delta("re")}\n\n",
            """data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}""" + "\n\n",
            "data: [DONE]\n\n",
        )
        val t = CannedTransport(raw)
        val seen = mutableListOf<String>()
        val turn = DenClient(t).chat(JSONObject().put("stream", true)) { chunk ->
            seen += chunk.getJSONArray("choices").getJSONObject(0).getJSONObject("delta").optString("content")
        }
        assertEquals(listOf("Hel", "lo the", "re", ""), seen)
        assertEquals("Hello there", turn.content)
        assertEquals("stop", turn.finishReason)
        assertTrue(t.sent.startsWith("POST /v1/chat/completions HTTP/1.1\r\n"))
    }

    @Test
    fun streamedToolCallsArePutBackTogether() {
        val raw = sseResponse(
            """data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "generate_image", "arguments": ""}}]}}]}""" + "\n\n",
            """data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"prompt\": \"a "}}]}}]}""" + "\n\n",
            """data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "cat\"}"}}]}}]}""" + "\n\n",
            """data: {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_b", "function": {"name": "generate_image", "arguments": "{\"prompt\": \"a dog\"}"}}]}, "finish_reason": "tool_calls"}]}""" + "\n\n",
            "data: [DONE]\n\n",
        )
        val turn = DenClient(CannedTransport(raw)).chat(JSONObject().put("stream", true))
        val calls = turn.toolCalls()
        assertEquals("tool_calls", turn.finishReason)
        assertEquals(2, calls.size)
        assertEquals(ToolCall("call_a", "generate_image", """{"prompt": "a cat"}"""), calls[0])
        assertEquals("""{"prompt": "a dog"}""", calls[1].arguments)
        assertEquals("", turn.content)
    }

    @Test
    fun nonStreamedAnswerIsReadWhole() {
        val body = """
            {"choices": [{"message": {"role": "assistant", "content": "hi",
              "tool_calls": [{"id": "c1", "function": {"name": "generate_image", "arguments": "{}"}}]},
              "finish_reason": "tool_calls"}]}
        """.trimIndent()
        val raw = "HTTP/1.1 200 OK\r\nContent-Length: ${body.toByteArray().size}\r\n\r\n$body"
        val t = CannedTransport(raw)
        val turn = DenClient(t).chat(JSONObject().put("stream", false))
        assertEquals("hi", turn.content)
        assertEquals("tool_calls", turn.finishReason)
        assertEquals(listOf(ToolCall("c1", "generate_image", "{}")), turn.toolCalls())
    }

    @Test
    fun anErrorEventStopsTheTurn() {
        val raw = sseResponse("""data: {"error": {"message": "den: model gone"}}""" + "\n\n")
        try {
            DenClient(CannedTransport(raw)).chat(JSONObject().put("stream", true))
            fail("expected a DenException")
        } catch (e: DenException) {
            assertEquals("den: model gone", e.message)
        }
    }

    @Test
    fun stopClosesAHangingStreamAndKeepsWhatCame() {
        val head = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
        val t = HangingTransport(head + chunk("data: ${delta("Hmm")}\n\n"))
        val stop = Stop()
        val seen = mutableListOf<String>()
        try {
            DenClient(t).chat(JSONObject().put("stream", true), stop) { chunk ->
                seen += chunk.getJSONArray("choices").getJSONObject(0).getJSONObject("delta").optString("content")
                // The user presses Stop from the UI thread while the model goes on thinking.
                Thread { stop.stop() }.start()
            }
            fail("expected a StoppedException")
        } catch (e: StoppedException) {
            assertEquals(listOf("Hmm"), seen)
            assertEquals(0L, t.closed.count)
        }
    }

    @Test
    fun aStopBeforeTheAnswerStartsClosesItOnArrival() {
        // Pressed while the model was still loading: the response is closed as soon as it opens.
        val head = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
        val t = HangingTransport(head)
        val stop = Stop().apply { stop() }
        try {
            DenClient(t).chat(JSONObject().put("stream", true), stop)
            fail("expected a StoppedException")
        } catch (e: StoppedException) {
            assertEquals(0L, t.closed.count)
        }
    }

    @Test
    fun aDroppedConnectionWithoutStopIsStillAnError() {
        val raw = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n" +
            chunk("data: ${delta("Hm")}\n\n")
        try {
            // Cut short: the chunked body ends without its last chunk.
            DenClient(CannedTransport(raw)).chat(JSONObject().put("stream", true), Stop())
            fail("expected a DenException")
        } catch (e: DenException) {
            assertTrue(e.message!!.startsWith("den: broker unreachable"))
        }
    }

    // --- the tool's schema and arguments ---

    @Test
    fun schemaFollowsThisDen() {
        val tools = ImageTool.tools(spec())!!
        val function = tools.getJSONObject(0).getJSONObject("function")
        assertEquals(ImageTool.NAME, function.getString("name"))
        val props = function.getJSONObject("parameters").getJSONObject("properties")
        val names = props.keys().asSequence().toList().sorted()
        assertEquals(listOf("cfg", "prompt", "sampler", "seed", "size", "steps", "workflow"), names)
        val workflows = props.getJSONObject("workflow").getJSONArray("enum")
        assertEquals("fast", workflows.getString(0))
        assertEquals("edit", workflows.getString(1))
        assertEquals(2, workflows.length())
        assertEquals(listOf("prompt"), listOf(function.getJSONObject("parameters").getJSONArray("required").getString(0)))
    }

    @Test
    fun noToolWhenImagesAreUnavailable() {
        val spec = spec()
        spec.getJSONObject("image").put("unavailable", "ComfyUI is not installed")
        assertNull(ImageTool.tools(spec))
    }

    @Test
    fun goodArgumentsBecomeAnImageRequest() {
        val request = ImageTool.request(spec(), """{"prompt": "a lake", "workflow": "edit", "size": "512x512", "steps": 8, "cfg": 3.5, "sampler": "euler"}""")
        assertEquals("a lake", request.getString("prompt"))
        assertEquals("edit", request.getString("workflow"))
        assertEquals(8L, request.getLong("steps"))
        assertEquals(3.5, request.getDouble("cfg"), 0.0001)
        assertEquals("512x512", request.getString("size"))
    }

    @Test
    fun badArgumentsComeBackAsMessagesForTheModel() {
        fun message(arguments: String): String = try {
            ImageTool.request(spec(), arguments)
            fail("expected a ToolArgumentException for $arguments")
            ""
        } catch (e: ToolArgumentException) {
            e.message!!
        }
        assertTrue(message("""{"prompt": "x", "workflow": "sdxl"}""").contains("workflow must be one of: fast, edit"))
        assertTrue(message("""{"prompt": "x", "quality": "high"}""").startsWith("unknown setting quality"))
        // A path is not a picture of this conversation: it is refused, never guessed at.
        assertTrue(message("""{"prompt": "x", "image": "/sdcard/a.png"}""").startsWith("no picture /sdcard/a.png"))
        assertTrue(message("""{"prompt": "x", "size": "big"}""").contains("WIDTHxHEIGHT"))
        assertTrue(message("""{"prompt": "x", "steps": "lots"}""").contains("whole number"))
        assertTrue(message("""{"workflow": "fast"}""").contains("prompt is required"))
        assertTrue(message("""not json""").contains("not valid JSON"))
    }

    // --- the context window ---

    @Test
    fun oldTurnsAreTrimmedAndToolResultsNeverLeadAlone() {
        fun message(role: String, text: String) = JSONObject().put("role", role).put("content", text)
        val messages = listOf(
            message("system", "s"),
            message("user", "old".repeat(200)),
            message("assistant", "call"),
            message("tool", "result".repeat(50)),
            message("user", "new"),
        )
        val kept = Context.fit(messages, numCtx = 600)
        assertEquals("system", kept.first().getString("role"))
        assertEquals("new", kept.last().getString("content"))
        assertEquals(listOf("system", "assistant", "tool", "user"), kept.map { it.getString("role") })

        // Tighter: the assistant call no longer fits, so its tool result goes with it.
        val tighter = Context.fit(messages, numCtx = 340)
        assertEquals(listOf("system", "user"), tighter.map { it.getString("role") })
        assertEquals("new", tighter.last().getString("content"))
    }
}

class HistoryTest {
    private fun conversation(): MutableList<ChatMessage> = mutableListOf(
        ChatMessage("user", "make me an image"),
        ChatMessage("assistant", "Sure.", toolCalls = org.json.JSONArray("""[{"id":"c1"}]""")),
        ChatMessage("tool", "Done: saved to Pictures/den/chat/c/den-1.png", toolCallId = "c1").also { message -> listOf("den-1.png").forEach { f -> message.pictures.add(Picture("img-1", f)) } },
        ChatMessage("assistant", "Here it is."),
    )

    @Test
    fun retryDropsTheWholeAnswerIncludingItsToolCalls() {
        val kept = History.withoutLastAnswer(conversation())
        assertEquals(listOf("user"), kept.map { it.role })
        assertEquals("make me an image", kept.last().content)
    }

    @Test
    fun resendTakesTheLastSentMessageBack() {
        val messages = conversation()
        messages.add(0, ChatMessage("assistant", "an older answer"))
        messages.add(0, ChatMessage("user", "an older question"))
        val (kept, sent) = History.withoutLastSent(messages)
        assertEquals(listOf("user", "assistant"), kept.map { it.role })
        assertEquals("make me an image", sent!!.content)
    }

    @Test
    fun resendKeepsTheImagesThatWentWithTheMessage() {
        val messages = mutableListOf(ChatMessage("user", "look").also { message -> listOf("sent-1.jpg").forEachIndexed { i, f -> message.pictures.add(Picture("img-${i + 1}", f)) } }, ChatMessage("assistant", "ok"))
        val (kept, sent) = History.withoutLastSent(messages)
        assertEquals(emptyList<String>(), kept.map { it.role })
        assertEquals(listOf("sent-1.jpg"), sent!!.images)
    }

    @Test
    fun bothAreOnlyOfferedWhenTheyMakeSense() {
        assertTrue(History.canRetry(conversation()))
        assertTrue(History.canResend(conversation()))
        // Nothing sent yet, and a turn that is still the user's.
        assertTrue(!History.canRetry(mutableListOf()))
        assertTrue(!History.canResend(mutableListOf()))
        assertTrue(!History.canRetry(mutableListOf(ChatMessage("user", "hi"))))
        assertTrue(History.canResend(mutableListOf(ChatMessage("user", "hi"))))
    }

    @Test
    fun retryingTwiceInARowIsStable() {
        val once = History.withoutLastAnswer(conversation())
        val twice = History.withoutLastAnswer(once)
        assertEquals(once.map { it.content }, twice.map { it.content })
    }
}

class ThinkingTest {
    private fun chunk(json: String) = JSONObject(json)

    @Test
    fun streamedThinkingIsKept() {
        val turn = Turn()
        turn.accept(chunk("""{"choices": [{"delta": {"role": "assistant", "content": ""}}]}"""))
        turn.accept(chunk("""{"choices": [{"delta": {"reasoning_content": "Let me "}}]}"""))
        turn.accept(chunk("""{"choices": [{"delta": {"reasoning_content": "think."}}]}"""))
        turn.accept(chunk("""{"choices": [{"delta": {"content": "Hello"}}]}"""))
        turn.accept(chunk("""{"choices": [{"delta": {}, "finish_reason": "stop"}]}"""))
        assertEquals("Let me think.", turn.thinking)
        assertEquals("Hello", turn.content)
    }

    @Test
    fun wholeMessageThinkingIsKept() {
        val turn = Turn()
        turn.acceptMessage(JSONObject("""{"role": "assistant", "content": "Hi", "reasoning_content": "Short thought."}"""))
        assertEquals("Short thought.", turn.thinking)
        assertEquals("Hi", turn.content)
    }

    @Test
    fun withoutThinkingTheFieldStaysEmpty() {
        val turn = Turn()
        turn.accept(chunk("""{"choices": [{"delta": {"content": "Hello"}}]}"""))
        turn.acceptMessage(JSONObject("""{"content": " there", "reasoning_content": null}"""))
        assertEquals("", turn.thinking)
        assertEquals("Hello there", turn.content)
    }

    @Test
    fun theThinkingFlagIsInTheRequestBothWays() {
        val history = listOf(ChatMessage("user", "hi"))
        for (on in listOf(true, false)) {
            for (stream in listOf(true, false)) {
                val body = ChatRequest.build(
                    model = "m", numCtx = 4096, systemPrompt = "s", history = history,
                    pictures = { emptyList() }, stream = stream, thinking = on,
                )
                assertEquals(on, body.getJSONObject("chat_template_kwargs").getBoolean("enable_thinking"))
                assertEquals(stream, body.getBoolean("stream"))
            }
        }
    }

    @Test
    fun aConversationCanAskForAnotherModel() {
        val body = ChatRequest.build(
            model = "other-model:9b", numCtx = 0, systemPrompt = "s",
            history = listOf(ChatMessage("user", "hi")), pictures = { emptyList() },
        )
        assertEquals("other-model:9b", body.getString("model"))
    }
}

class PictureIdTest {
    private fun spec() = JSONObject(
        """
        {"image": {"unavailable": null, "default": "fast", "workflows": ["fast", "edit"], "edits": ["edit"],
          "listing": "* fast",
          "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string"}, "workflow": {"type": "string", "enum": ["fast", "edit"]},
            "seed": {"type": "integer"}, "image": {"type": "string"}}}}}
        """.trimIndent(),
    )

    private val ids = listOf("img-1", "img-2", "img-4")

    @Test
    fun theToolNamesThePicturesThatExist() {
        val props = ImageTool.tools(spec(), ids)!!
            .getJSONObject(0).getJSONObject("function").getJSONObject("parameters").getJSONObject("properties")
        val which = props.getJSONObject(ImageTool.WHICH).getString("description")
        assertTrue(which, which.contains("img-1, img-2, img-4"))
        assertTrue(which, which.contains("\"all\""))
        // A conversation with no pictures yet has nothing to edit.
        val none = ImageTool.tools(spec(), emptyList())!!
            .getJSONObject(0).getJSONObject("function").getJSONObject("parameters").getJSONObject("properties")
        assertTrue(!none.has(ImageTool.WHICH))
    }

    @Test
    fun aPictureIsPickedByItsId() {
        val request = ImageTool.request(spec(), """{"prompt": "warmer", "image": "img-2"}""", ids)
        assertEquals(listOf("img-2"), ImageTool.targets(request, listOf("img-4")))
    }

    @Test
    fun severalIdsInOneCall() {
        val request = ImageTool.request(spec(), """{"prompt": "warmer", "image": "img-1, img-4"}""", ids)
        assertEquals(listOf("img-1", "img-4"), ImageTool.targets(request, emptyList()))
    }

    @Test
    fun allMeansThePicturesOfTheLastMessage() {
        val request = ImageTool.request(spec(), """{"prompt": "warmer", "image": "all"}""", ids)
        assertEquals(listOf("img-2", "img-4"), ImageTool.targets(request, listOf("img-2", "img-4")))
    }

    @Test
    fun noIdMeansANewPicture() {
        val request = ImageTool.request(spec(), """{"prompt": "a bird"}""", ids)
        assertEquals(emptyList<String>(), ImageTool.targets(request, listOf("img-4")))
    }

    @Test
    fun anUnknownIdListsTheOnesThereAre() {
        fun message(arguments: String, known: List<String>): String = try {
            ImageTool.request(spec(), arguments, known)
            fail("expected a ToolArgumentException")
            ""
        } catch (e: ToolArgumentException) {
            e.message!!
        }
        val unknown = message("""{"prompt": "x", "image": "img-3"}""", ids)
        assertTrue(unknown, unknown.contains("no picture img-3"))
        assertTrue(unknown, unknown.contains("img-1, img-2, img-4"))
        // Never a silent fallback to the last picture.
        assertTrue(message("""{"prompt": "x", "image": "the second one"}""", ids).contains("no picture"))
        assertTrue(message("""{"prompt": "x", "image": "img-1, img-9"}""", ids).contains("no picture img-9"))
        assertTrue(message("""{"prompt": "x", "image": "img-1"}""", emptyList()).contains("none yet"))
    }

    @Test
    fun everyPictureIsNamedForTheModel() {
        val message = ChatMessage("user", "what are these?")
        message.pictures.add(Picture("img-5", "a.jpg"))
        message.pictures.add(Picture("img-6", "b.jpg"))
        val body = ChatRequest.build(
            model = "m", numCtx = 65536, systemPrompt = "s", history = listOf(message),
            pictures = { it.pictures.map { picture -> picture.id to "data:image/jpeg;base64,${picture.file}" } },
        )
        val parts = (body.getJSONArray("messages").getJSONObject(1).get("content") as JSONArray)
        val texts = (0 until parts.length()).map { parts.getJSONObject(it) }
            .filter { it.getString("type") == "text" }.map { it.getString("text") }
        assertEquals(listOf("what are these?", "[img-5]", "[img-6]"), texts)
    }
}

class ConversationIdsTest {
    @Test
    fun idsCountUpAndAreNeverReused() {
        val conversation = Conversation(id = "c", title = "t")
        val first = conversation.newPictureId()
        val second = conversation.newPictureId()
        assertEquals(listOf("img-1", "img-2"), listOf(first, second))
        val message = ChatMessage("user", "look")
        message.pictures.add(Picture(first, "a.jpg"))
        message.pictures.add(Picture(second, "b.jpg"))
        conversation.messages.add(message)
        // Dropping the turn (a retry) must not hand its numbers out again.
        conversation.messages.clear()
        assertEquals("img-3", conversation.newPictureId())
    }

    @Test
    fun aPictureIsFoundWhereverItIsInTheConversation() {
        val conversation = Conversation(id = "c", title = "t")
        val old = ChatMessage("user", "first")
        old.pictures.add(Picture(conversation.newPictureId(), "a.jpg"))
        val made = ChatMessage("tool", "Done.")
        made.pictures.add(Picture(conversation.newPictureId(), "den-1.png"))
        conversation.messages.add(old)
        conversation.messages.add(ChatMessage("assistant", "ok"))
        conversation.messages.add(made)
        assertEquals(listOf("img-1", "img-2"), conversation.pictures().map { it.id })
        assertEquals("den-1.png", conversation.picture("img-2")!!.file)
        assertNull(conversation.picture("img-9"))
    }

    @Test
    fun idsSurviveBeingStoredAndReadBack() {
        val conversation = Conversation(id = "c", title = "t")
        val message = ChatMessage("user", "look")
        message.pictures.add(Picture(conversation.newPictureId(), "a.jpg"))
        message.pictures.add(Picture(conversation.newPictureId(), "b.jpg"))
        conversation.messages.add(message)
        val read = Conversation.fromJson(JSONObject(conversation.toJson().toString()))
        assertEquals(listOf("img-1", "img-2"), read.pictures().map { it.id })
        assertEquals("img-3", read.newPictureId()) // the counter came back too
    }

    @Test
    fun aConversationStoredBeforeIdsGetsThem() {
        val legacy = JSONObject(
            """
            {"id": "c", "title": "t", "messages": [
              {"role": "user", "content": "look", "images": ["a.jpg", "b.jpg"]},
              {"role": "tool", "content": "Done.", "images": ["den-1.png"]}]}
            """.trimIndent(),
        )
        val read = Conversation.fromJson(legacy)
        assertEquals(listOf("img-1", "img-2", "img-3"), read.pictures().map { it.id })
        assertEquals(listOf("a.jpg", "b.jpg", "den-1.png"), read.pictures().map { it.file })
        assertEquals("img-4", read.newPictureId())
    }
}
