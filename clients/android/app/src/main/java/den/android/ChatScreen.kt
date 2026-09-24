package den.android

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.isImeVisible
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.gestures.scrollBy
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLinkStyles
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withLink
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import kotlinx.coroutines.delay

@OptIn(ExperimentalLayoutApi::class)
/** Puts the end of the last message at the bottom of the list, however tall that message is. */
private suspend fun LazyListState.showNewest(lastIndex: Int) {
    if (lastIndex < 0) return
    scrollToItem(lastIndex)
    scrollBy(layoutInfo.viewportSize.height.toFloat())
}

@Composable
fun ChatScreen(model: AppModel, modifier: Modifier) {
    LaunchedEffect(Unit) { model.loadConversations() }
    val conversation = model.chat
    if (conversation == null) ChatList(model, modifier) else ChatPane(model, conversation, modifier)
}

@Composable
private fun ChatList(model: AppModel, modifier: Modifier) {
    var deleting by remember { mutableStateOf<Conversation?>(null) }
    Column(modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("Chat", style = MaterialTheme.typography.headlineSmall)
        if (model.client == null) {
            Text("Not connected. Connect to a den on the Connect tab first.")
            return@Column
        }
        Button(onClick = model::newChat) { Text("New chat") }
        if (model.conversations.isEmpty()) Text("No conversations yet.")
        LazyColumn(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            items(model.conversations.size) { i ->
                val conversation = model.conversations[i]
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        conversation.title,
                        Modifier.weight(1f).clickable { model.openChat(conversation) }.padding(vertical = 12.dp),
                    )
                    TextButton(onClick = { deleting = conversation }) { Text("Delete") }
                }
                HorizontalDivider()
            }
        }
    }
    deleting?.let { conversation ->
        val images = remember(conversation.id) { model.galleryCount(conversation) }
        AlertDialog(
            onDismissRequest = { deleting = null },
            title = { Text("Delete \"${conversation.title}\"?") },
            text = {
                Text(
                    if (images > 0) "Its $images image(s) are in the gallery under Pictures/den/chat/${conversation.folder}/. Delete those too?"
                    else "It has no images in the gallery."
                )
            },
            confirmButton = {
                TextButton(onClick = { model.deleteChat(conversation, withImages = true); deleting = null }) {
                    Text(if (images > 0) "Delete with images" else "Delete")
                }
            },
            dismissButton = {
                Row {
                    if (images > 0) {
                        TextButton(onClick = { model.deleteChat(conversation, withImages = false); deleting = null }) {
                            Text("Keep images")
                        }
                    }
                    TextButton(onClick = { deleting = null }) { Text("Cancel") }
                }
            },
        )
    }
}

@Composable
@OptIn(ExperimentalLayoutApi::class)
private fun ChatPane(model: AppModel, conversation: Conversation, modifier: Modifier) {
    val tick = model.chatTick // the messages are plain objects; this is what recomposes them
    var renaming by remember { mutableStateOf(false) }
    var title by remember(conversation.id) { mutableStateOf(conversation.title) }
    var settingsOpen by remember { mutableStateOf(false) }
    val listState = rememberLazyListState()
    // Keep the newest message in sight: on a new message or token, when the keyboard opens or
    // closes, and while a long draft grows the input box.
    val imeVisible = WindowInsets.isImeVisible
    LaunchedEffect(tick, imeVisible, model.draft) {
        listState.showNewest(conversation.messages.lastIndex)
        // The keyboard takes its height over a few frames; land at the bottom once it is there.
        delay(300)
        listState.showNewest(conversation.messages.lastIndex)
    }
    Column(modifier.fillMaxSize().padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = model::closeChat) { Text("← chats") }
            Text(conversation.title, Modifier.weight(1f), style = MaterialTheme.typography.titleMedium, maxLines = 1)
            TextButton(onClick = { renaming = true }) { Text("Rename") }
            TextButton(onClick = { settingsOpen = !settingsOpen }) { Text("⚙") }
        }
        conversation.model?.takeIf { it != model.defaultModel() }?.let {
            Text("model: $it", style = MaterialTheme.typography.labelSmall)
        }
        // A session that dropped shouldn't be a mystery; the next message reopens it.
        when (model.link) {
            ConnectionState.RECONNECTING -> Text("reconnecting…", style = MaterialTheme.typography.labelSmall)
            ConnectionState.DISCONNECTED -> Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text(
                    "disconnected — the next message tries again",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.error,
                )
                TextButton(onClick = model::reconnect) { Text("Retry") }
            }
            ConnectionState.CONNECTED -> Unit
        }
        if (settingsOpen) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Thinking", Modifier.weight(1f))
                Switch(checked = model.thinking, onCheckedChange = model::toggleThinking)
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Stream while the image tool is offered", Modifier.weight(1f))
                Switch(checked = model.streamWithTools, onCheckedChange = model::toggleStreamWithTools)
            }
            ModelPicker(model, conversation)
            Text(
                "Context ${model.status?.opt("num_ctx") ?: "?"} tokens.",
                style = MaterialTheme.typography.bodySmall,
            )
        }
        // A fresh list again, so the chips follow what the conversation holds now. While the
        // keyboard is up, the room goes to the messages and the field instead.
        if (!imeVisible) Skills(model, conversation.attachments.toList()) { model.detachSkill(it) }
        // A fresh list each time the tick changes: the messages are plain objects, so the lazy
        // list needs something that differs to rebuild its items while an answer streams in.
        val messages = conversation.messages.toList()
        LazyColumn(Modifier.weight(1f), state = listState, verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(messages.size) { i ->
                val message = messages[i]
                // The last answer while a turn runs is the one still being written.
                val streaming = model.chatBusy && i == messages.lastIndex && message.role == "assistant"
                Bubble(model, conversation, message, message.content, message.note, message.thinking, streaming)
            }
        }
        model.chatError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        // Taking the end back: another answer to the same question, or the question itself again.
        if (!model.chatBusy && (History.canRetry(conversation.messages) || History.canResend(conversation.messages))) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (History.canRetry(conversation.messages)) {
                    TextButton(onClick = model::retry) { Text("↻ Retry") }
                }
                if (History.canResend(conversation.messages)) {
                    TextButton(onClick = model::resend) { Text("✎ Edit & resend") }
                }
            }
        }
        Attachments(model)
        val pick = rememberLauncherForActivityResult(ActivityResultContracts.PickMultipleVisualMedia(4)) { uris ->
            uris.forEach { model.attachImage(it, it.lastPathSegment ?: "image") }
        }
        var photo by remember { mutableStateOf<android.net.Uri?>(null) }
        val camera = rememberLauncherForActivityResult(ActivityResultContracts.TakePicture()) { taken ->
            if (taken) photo?.let { model.attachImage(it, "photo.jpg") }
        }
        val gallery = { pick.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)) }
        val takePhoto = {
            val target = model.cameraTarget()
            photo = target
            runCatching { camera.launch(target) }.onFailure { model.chatError = "no camera app here" }
            Unit
        }
        // Something to send? Then the button at the end sends, and the one at the start attaches.
        val ready = model.draft.isNotBlank() || model.pending.isNotEmpty()
        Row(verticalAlignment = Alignment.CenterVertically) {
            // Two paperclips at once would be silly: the one at the start is there for when the
            // one at the end has turned into Send.
            if (ready) AttachButton(gallery, takePhoto)
            OutlinedTextField(
                model.draft, { model.draft = it }, Modifier.weight(1f),
                label = { Text("Message") }, maxLines = 4,
            )
            when {
                // The spinner says it's working; the square inside it stops the turn.
                model.chatBusy -> Box(Modifier.size(48.dp), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                    IconButton(onClick = model::stopTurn) {
                        Icon(painterResource(R.drawable.ic_stop), "Stop", Modifier.size(16.dp), tint = MaterialTheme.colorScheme.primary)
                    }
                }
                ready -> IconButton(onClick = model::send) {
                    Icon(painterResource(R.drawable.ic_send), "Send", tint = MaterialTheme.colorScheme.primary)
                }
                else -> AttachButton(gallery, takePhoto)
            }
        }
    }
    if (renaming) {
        AlertDialog(
            onDismissRequest = { renaming = false },
            title = { Text("Rename chat") },
            text = { OutlinedTextField(title, { title = it }, singleLine = true) },
            confirmButton = { TextButton(onClick = { model.renameChat(title); renaming = false }) { Text("Rename") } },
            dismissButton = { TextButton(onClick = { renaming = false }) { Text("Cancel") } },
        )
    }
}

/** The skills of the broker's den: picked one at a time, shown as chips with what they cost. */
@Composable
private fun Skills(model: AppModel, attachments: List<Attachment>, onRemove: (Attachment) -> Unit) {
    var open by remember { mutableStateOf(false) }
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Box {
                OutlinedButton(onClick = { open = true; model.loadSkills() }) { Text("Skills ▾") }
                DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
                    if (model.skillsLoading) DropdownMenuItem(text = { Text("loading…") }, onClick = {})
                    if (!model.skillsLoading && model.skillList.isEmpty()) {
                        DropdownMenuItem(text = { Text("this den offers no skills") }, onClick = { open = false })
                    }
                    model.skillList.forEach { skill ->
                        val name = skill.text("name")
                        DropdownMenuItem(
                            text = {
                                Column {
                                    Text(name)
                                    Text(
                                        skill.text("description").take(120),
                                        style = MaterialTheme.typography.bodySmall,
                                        maxLines = 3,
                                    )
                                }
                            },
                            onClick = { model.attachSkill(name); open = false },
                        )
                        val references = skill.optJSONArray("references")
                        (0 until (references?.length() ?: 0)).forEach { i ->
                            val reference = references!!.getString(i)
                            DropdownMenuItem(
                                text = { Text("     $name · $reference", style = MaterialTheme.typography.bodySmall) },
                                onClick = { model.attachSkill(name, reference); open = false },
                            )
                        }
                    }
                }
            }
            val attached = attachments.sumOf { it.tokens }
            if (attached > 0) Text("~$attached tokens in the prompt", style = MaterialTheme.typography.bodySmall)
            model.skillsError?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error) }
        }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            attachments.forEach { attachment ->
                AssistChip(
                    onClick = { onRemove(attachment) },
                    label = { Text("${attachment.label} ~${attachment.tokens} ✕") },
                )
            }
        }
    }
}


/** Pictures waiting to go with the next message: see them, take them away again. */
@Composable
private fun Attachments(model: AppModel) {
    if (model.pending.isEmpty()) return
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            model.pending.toList().forEach { attached ->
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    attached.bitmap?.let {
                        Image(
                            it.asImageBitmap(), attached.name,
                            Modifier.size(72.dp).clickable { model.removeAttachment(attached) },
                            contentScale = ContentScale.Crop,
                        )
                    }
                    Text(
                        "${attached.width}x${attached.height}" + if (attached.scaled) " scaled ✕" else " ✕",
                        style = MaterialTheme.typography.labelSmall,
                        modifier = Modifier.clickable { model.removeAttachment(attached) },
                    )
                }
            }
        }
        Text(
            "Sent at up to ${Media.LONG_SIDE} px as JPEG, to keep the model's context small.",
            style = MaterialTheme.typography.labelSmall,
        )
    }
}

/**
 * Which model this conversation asks for. The den's own selected one is the default; another
 * costs a swap on that machine, so the picker says so before it is chosen.
 */
@Composable
private fun ModelPicker(model: AppModel, conversation: Conversation) {
    var open by remember { mutableStateOf(false) }
    val selected = model.defaultModel()
    val using = conversation.model ?: selected ?: "?"
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Box {
            OutlinedButton(onClick = { open = true; model.loadModels() }, modifier = Modifier.fillMaxWidth()) {
                Text("Model: $using" + if (conversation.model == null) " (den's own)" else "")
            }
            DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
                DropdownMenuItem(
                    text = { Text("${selected ?: "the den's selected model"} — den's own") },
                    onClick = { model.useModel(null); open = false },
                )
                if (model.modelsLoading) DropdownMenuItem(text = { Text("loading…") }, onClick = {})
                model.models.filter { it != selected }.forEach { name ->
                    DropdownMenuItem(text = { Text(name) }, onClick = { model.useModel(name); open = false })
                }
            }
        }
        Text(
            "Another model means the den unloads this one and loads that one: tens of seconds, " +
                "and its memory, on that machine.",
            style = MaterialTheme.typography.labelSmall,
        )
    }
}

/** The paperclip: a menu of where a picture comes from. Same size as the send button. */
@Composable
private fun AttachButton(onGallery: () -> Unit, onCamera: () -> Unit) {
    var open by remember { mutableStateOf(false) }
    Box {
        IconButton(onClick = { open = true }) {
            Icon(painterResource(R.drawable.ic_attach), "Attach image")
        }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            DropdownMenuItem(
                text = { Text("Gallery") },
                leadingIcon = { Icon(painterResource(R.drawable.ic_gallery), null) },
                onClick = { open = false; onGallery() },
            )
            DropdownMenuItem(
                text = { Text("Camera") },
                leadingIcon = { Icon(painterResource(R.drawable.ic_camera), null) },
                onClick = { open = false; onCamera() },
            )
        }
    }
}

/** A message's text with its links tappable, and a long press to copy the whole message. */
@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun LinkedText(text: String, style: androidx.compose.ui.text.TextStyle, onLink: (Link) -> Unit) {
    val clipboard = LocalClipboardManager.current
    val linkStyle = SpanStyle(color = MaterialTheme.colorScheme.primary, textDecoration = TextDecoration.Underline)
    val annotated = buildAnnotatedString {
        Links.split(text).forEach { (piece, link) ->
            if (link == null) {
                append(piece)
            } else {
                withLink(LinkAnnotation.Clickable(link.text, TextLinkStyles(linkStyle)) { onLink(link) }) { append(piece) }
            }
        }
    }
    Text(
        annotated,
        style = style,
        modifier = Modifier.combinedClickable(
            onClick = {},
            onLongClick = { clipboard.setText(AnnotatedString(text)) },
        ),
    )
}

/** What tapping a link offers: a copy always, and opening it when this phone can. */
@Composable
private fun LinkMenu(link: Link, conversation: Conversation, model: AppModel, onDismiss: () -> Unit) {
    val context = LocalContext.current
    val clipboard = LocalClipboardManager.current
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(if (link.kind == LinkKind.URL) "Link" else "File") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(link.text, style = MaterialTheme.typography.bodySmall)
                if (link.kind == LinkKind.OTHER_MACHINE_PATH) {
                    Text("This file is on den's machine, so it can only be copied from here.")
                }
            }
        },
        confirmButton = {
            TextButton(onClick = { clipboard.setText(AnnotatedString(link.text)); onDismiss() }) { Text("Copy") }
        },
        dismissButton = {
            if (link.kind != LinkKind.OTHER_MACHINE_PATH) {
                TextButton(onClick = {
                    val opened = runCatching {
                        when (link.kind) {
                            LinkKind.URL -> Media.open(context, link.text)
                            else -> {
                                val uri = Media.inGallery(context, link.text)
                                    ?: throw IllegalStateException("not in the gallery any more")
                                Media.view(context, uri)
                            }
                        }
                    }
                    if (opened.isFailure) model.chatError = "could not open ${link.text}: ${opened.exceptionOrNull()?.message}"
                    onDismiss()
                }) { Text("Open") }
            } else {
                TextButton(onClick = onDismiss) { Text("Close") }
            }
        },
    )
}

/** One picture of the conversation, full screen, with a copy and a share. */
@Composable
private fun ImageViewer(model: AppModel, conversation: Conversation, name: String, onDismiss: () -> Unit) {
    val context = LocalContext.current
    val bitmap = model.imageOf(conversation, name)
    Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(color = MaterialTheme.colorScheme.scrim.copy(alpha = 0.92f)) {
            Column(
                Modifier.fillMaxSize().padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp, Alignment.CenterVertically),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                    bitmap?.let {
                        Image(it.asImageBitmap(), name, Modifier.fillMaxWidth(), contentScale = ContentScale.Fit)
                    }
                }
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    TextButton(onClick = {
                        val uri = Media.share(context, model.chats.imageFile(conversation, name))
                        val clip = android.content.ClipData.newUri(context.contentResolver, name, uri)
                        val manager = context.getSystemService(android.content.ClipboardManager::class.java)
                        manager?.setPrimaryClip(clip)
                        onDismiss()
                    }) { Text("Copy") }
                    TextButton(onClick = {
                        val uri = Media.share(context, model.chats.imageFile(conversation, name))
                        runCatching { Media.sendTo(context, uri, if (name.endsWith(".jpg")) Media.MIME else "image/png") }
                        onDismiss()
                    }) { Text("Share") }
                    TextButton(onClick = onDismiss) { Text("Close") }
                }
            }
        }
    }
}

/**
 * What the model thought, set apart from what it said: its own dimmer surface, smaller type and
 * a rule down the side. It stands open while the thinking is all there is, and folds away by
 * itself the moment the answer starts — unless the reader has opened or closed it by hand. The
 * header is a row you tap, with a chevron that turns, so it is plain that it folds.
 */
@Composable
private fun Thinking(thinking: String, openWhileWriting: Boolean, streaming: Boolean) {
    var byHand by remember { mutableStateOf<Boolean?>(null) }
    val open = byHand ?: openWhileWriting
    var seconds by remember { mutableIntStateOf(0) }
    LaunchedEffect(streaming) {
        seconds = 0
        while (streaming) {
            delay(1000)
            seconds++
        }
    }
    val turn by animateFloatAsState(if (open) 180f else 0f, label = "chevron")
    val describe = if (open) "Hide the model's thinking" else "Show the model's thinking"
    Surface(
        color = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.6f),
        shape = MaterialTheme.shapes.small,
        modifier = Modifier.fillMaxWidth(),
    ) {
        Row(Modifier.padding(start = 2.dp)) {
            // A rule down the side, so the block reads as an aside even when it is open.
            Box(Modifier.width(3.dp).fillMaxHeight().background(MaterialTheme.colorScheme.outlineVariant))
            Column(Modifier.padding(horizontal = 6.dp, vertical = 4.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Surface(
                    onClick = { byHand = !open },
                    color = MaterialTheme.colorScheme.secondaryContainer.copy(alpha = 0.5f),
                    shape = MaterialTheme.shapes.small,
                    modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp).semantics { contentDescription = describe },
                ) {
                    Row(
                        Modifier.padding(horizontal = 10.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        if (streaming) CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 2.dp)
                        Text(
                            "Thinking" + if (streaming) " · ${seconds}s" else " · ${thinking.length} chars",
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.weight(1f),
                        )
                        Text(
                            if (open) "Hide" else "Show",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        Icon(
                            painterResource(R.drawable.ic_chevron),
                            null,
                            Modifier.size(20.dp).rotate(turn),
                            tint = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
                if (open) {
                    SelectionContainer {
                        Text(
                            thinking,
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(horizontal = 4.dp, vertical = 2.dp),
                        )
                    }
                }
            }
        }
    }
    if (open) HorizontalDivider()
}

@Composable
private fun Bubble(
    model: AppModel,
    conversation: Conversation,
    message: ChatMessage,
    content: String,
    note: String?,
    thinking: String,
    streaming: Boolean = false,
) {
    val colors = when (message.role) {
        "user" -> CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer)
        "tool" -> CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
        else -> CardDefaults.cardColors()
    }
    var tapped by remember { mutableStateOf<Link?>(null) }
    var viewing by remember { mutableStateOf<String?>(null) }
    tapped?.let { LinkMenu(it, conversation, model) { tapped = null } }
    viewing?.let { ImageViewer(model, conversation, it) { viewing = null } }
    Card(colors = colors, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            if (message.role != "user") {
                Text(
                    if (message.role == "tool") "${message.toolName ?: "tool"}" else "den",
                    style = MaterialTheme.typography.labelMedium,
                )
            }
            note?.let {
                if (message.role == "tool") {
                    // A tool's note is what it is doing right now.
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        CircularProgressIndicator(Modifier.padding(2.dp))
                        Text(it, style = MaterialTheme.typography.bodySmall)
                    }
                } else {
                    // A sent message's note says what went with it.
                    Text(it, style = MaterialTheme.typography.labelSmall)
                }
            }
            message.pictures.forEach { picture ->
                model.imageOf(conversation, picture.file)?.let {
                    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                        Image(
                            it.asImageBitmap(), picture.id,
                            Modifier.fillMaxWidth().clickable { viewing = picture.file },
                            contentScale = ContentScale.FillWidth,
                        )
                        // The id is what the user and the model call this picture.
                        Text(
                            picture.id,
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }
            if (thinking.isNotBlank()) {
                Thinking(thinking, streaming && content.isBlank(), streaming)
            }
            if (content.isNotBlank()) {
                LinkedText(
                    content,
                    if (message.role == "tool") MaterialTheme.typography.bodySmall else MaterialTheme.typography.bodyMedium,
                ) { tapped = it }
            }
            if (content.isBlank() && !streaming && thinking.isNotBlank() && message.toolCalls == null) {
                Text(
                    "The model thought but sent no answer — Retry asks it again.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }
            message.toolCalls?.let { calls ->
                val names = (0 until calls.length()).joinToString(", ") { calls.getJSONObject(it).optJSONObject("function")?.optString("name").orEmpty() }
                Text("called $names", style = MaterialTheme.typography.labelSmall)
            }
        }
    }
}
