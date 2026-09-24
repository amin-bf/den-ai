package den.android

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RequestTest {
    private val picture = "data:image/jpeg;base64,AAAA"

    private fun build(history: List<ChatMessage>, numCtx: Int) = ChatRequest.build(
        model = "m", numCtx = numCtx, systemPrompt = "You are den.",
        history = history,
        pictures = { message -> message.pictures.map { it.id to picture } },
    )

    private fun JSONObject.messages(): List<JSONObject> =
        getJSONArray("messages").let { a -> (0 until a.length()).map { a.getJSONObject(it) } }

    @Test
    fun theVeryFirstMessageOfAChatKeepsItsPicture() {
        // A new conversation: one message, an image, and a den whose num_ctx isn't known yet.
        val history = listOf(ChatMessage("user", "what is this?").also { message -> listOf("sent-1.jpg").forEachIndexed { i, f -> message.pictures.add(Picture("img-${i + 1}", f)) } })
        for (numCtx in listOf(0, 4096, 65536)) {
            val body = build(history, numCtx)
            val messages = body.messages()
            assertEquals(listOf("system", "user"), messages.map { it.getString("role") })
            val parts = messages[1].get("content") as JSONArray
            val kinds = (0 until parts.length()).map { parts.getJSONObject(it).getString("type") }
            assertEquals(listOf("text", "text", "image_url"), kinds) // the words, the id, the picture
            assertEquals("[img-1]", parts.getJSONObject(1).getString("text"))
            assertEquals(picture, parts.getJSONObject(2).getJSONObject("image_url").getString("url"))
            assertTrue(ChatRequest.carriesPictures(body))
        }
    }

    @Test
    fun threePicturesGoInOneMessageInTheOrderTheyWereAttached() {
        val message = ChatMessage("user", "what is in these?").also { message -> listOf("sent-1.jpg", "sent-2.jpg", "sent-3.jpg").forEachIndexed { i, f -> message.pictures.add(Picture("img-${i + 1}", f)) } }
        val body = ChatRequest.build(
            model = "m", numCtx = 8192, systemPrompt = "You are den.", history = listOf(message),
            pictures = { it.pictures.map { picture -> picture.id to "data:image/jpeg;base64,${picture.file}" } },
        )
        val messages = body.messages()
        assertEquals("one user message, not three", listOf("system", "user"), messages.map { it.getString("role") })
        val parts = messages[1].get("content") as JSONArray
        assertEquals("text", parts.getJSONObject(0).getString("type"))
        val pictures = (0 until parts.length()).map { parts.getJSONObject(it) }
            .filter { it.getString("type") == "image_url" }
        assertEquals(3, pictures.size)
        assertEquals(
            listOf("data:image/jpeg;base64,sent-1.jpg", "data:image/jpeg;base64,sent-2.jpg", "data:image/jpeg;base64,sent-3.jpg"),
            pictures.map { it.getJSONObject("image_url").getString("url") },
        )
    }

    @Test
    fun threePicturesSurviveTheTrimming() {
        // Three pictures are about 3600 tokens; a small context must not leave only the last.
        val message = ChatMessage("user", "all three please").also { message -> listOf("a.jpg", "b.jpg", "c.jpg").forEachIndexed { i, f -> message.pictures.add(Picture("img-${i + 1}", f)) } }
        for (numCtx in listOf(0, 8192, 65536)) {
            val body = ChatRequest.build(
                model = "m", numCtx = numCtx, systemPrompt = "s", history = listOf(message),
                pictures = { it.pictures.map { picture -> picture.id to "data:image/jpeg;base64,${picture.file}" } },
            )
            val parts = body.messages().last().get("content") as JSONArray
            assertEquals("with num_ctx $numCtx", 3, (0 until parts.length()).count { parts.getJSONObject(it).getString("type") == "image_url" })
        }
    }

    @Test
    fun picturesOfOlderTurnsAreStillSentWithTheHistory() {
        val history = listOf(
            ChatMessage("user", "first").also { message -> listOf("a.jpg", "b.jpg").forEachIndexed { i, f -> message.pictures.add(Picture("img-${i + 1}", f)) } },
            ChatMessage("assistant", "I see two."),
            ChatMessage("user", "and now?"),
        )
        val body = ChatRequest.build(
            model = "m", numCtx = 65536, systemPrompt = "s", history = history,
            pictures = { it.pictures.map { picture -> picture.id to "data:image/jpeg;base64,${picture.file}" } },
        )
        val first = body.messages()[1].get("content") as JSONArray
        val pictures = (0 until first.length()).map { first.getJSONObject(it) }.count { it.getString("type") == "image_url" }
        assertEquals("both pictures go with the history", 2, pictures)
    }

    @Test
    fun aPictureWithoutWordsIsStillSent() {
        val body = build(listOf(ChatMessage("user", "").also { message -> listOf("sent-1.jpg").forEachIndexed { i, f -> message.pictures.add(Picture("img-${i + 1}", f)) } }), 0)
        val parts = body.messages()[1].get("content") as JSONArray
        // No words, so only the picture's id and the picture itself.
        assertEquals(listOf("text", "image_url"), (0 until parts.length()).map { parts.getJSONObject(it).getString("type") })
        assertEquals("[img-1]", parts.getJSONObject(0).getString("text"))
    }

    @Test
    fun aPictureThatCannotBeReadLeavesTheTextAlone() {
        val body = ChatRequest.build(
            model = "m", numCtx = 0, systemPrompt = "s",
            history = listOf(ChatMessage("user", "look").also { message -> listOf("gone.jpg").forEachIndexed { i, f -> message.pictures.add(Picture("img-${i + 1}", f)) } }),
            pictures = { emptyList() },
        )
        assertEquals("look", body.messages()[1].getString("content"))
        assertFalse(ChatRequest.carriesPictures(body))
    }

    @Test
    fun whatDenGeneratedIsNotSentBackAsPixels() {
        val history = listOf(
            ChatMessage("user", "draw a bird"),
            ChatMessage("tool", "Done.", toolCallId = "c1").also { message -> listOf("den-1.png").forEach { f -> message.pictures.add(Picture("img-9", f)) } },
        )
        val body = build(history, 65536)
        assertEquals("Done.", body.messages().last().getString("content"))
        assertFalse(ChatRequest.carriesPictures(body))
    }

    @Test
    fun aFailureIsNamedForWhatItWas() {
        val closed = "den: broker unreachable through the SSH session (the SSH connection is closed; reconnect)"
        assertTrue(Failures.chat(closed, carriedPictures = true).startsWith("the connection to den dropped"))
        assertFalse(Failures.chat(closed, carriedPictures = true).contains("no vision"))

        val noVision = "den: image input is not supported by this server (no mmproj)"
        assertTrue(Failures.chat(noVision, carriedPictures = true).contains("no vision"))
        assertTrue(Failures.chat(noVision, carriedPictures = true).contains(noVision))

        val other = "den: the model is not loaded"
        assertEquals("the den could not answer: $other", Failures.chat(other, carriedPictures = true))
        assertEquals("the den could not answer: $other", Failures.chat(other, carriedPictures = false))
        assertTrue(Failures.chat(null, carriedPictures = true).contains("no reason given"))
    }

    @Test
    fun orientationTagsBecomeTurns() {
        assertEquals(Media.Upright(0, false), Media.upright(1))
        assertEquals(Media.Upright(1, false), Media.upright(6))
        assertEquals(Media.Upright(2, false), Media.upright(3))
        assertEquals(Media.Upright(3, false), Media.upright(8))
        assertEquals(Media.Upright(0, true), Media.upright(2))
        assertEquals(Media.Upright(2, true), Media.upright(4))
        assertEquals(Media.Upright(3, true), Media.upright(5))
        assertEquals(Media.Upright(1, true), Media.upright(7))
        assertEquals(Media.Upright(0, false), Media.upright(0))
        assertEquals(90f, Media.upright(6).degrees, 0.001f)
    }
}
