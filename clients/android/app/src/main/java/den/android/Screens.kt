package den.android

import android.Manifest
import android.content.Intent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import org.json.JSONObject

// --- shared pieces ---

@Composable
private fun Page(modifier: Modifier, content: @Composable ColumnScope.() -> Unit) {
    Column(
        modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
        content = content,
    )
}

@Composable
private fun Title(text: String) = Text(text, style = MaterialTheme.typography.headlineSmall)

@Composable
private fun ErrorText(text: String?) {
    if (text != null) Text(text, color = MaterialTheme.colorScheme.error)
}

@Composable
private fun Mono(text: String) = SelectionContainer {
    Text(text, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
}

@Composable
private fun NotConnected(model: AppModel): Boolean {
    if (model.client != null && model.connectedTo != null) return false
    Text("Not connected. Connect to a den on the Connect tab first.")
    return true
}

@Composable
private fun Picker(label: String, value: String?, options: List<String>, onPick: (String?) -> Unit, allowNone: Boolean = false) {
    var open by remember { mutableStateOf(false) }
    Box {
        OutlinedButton(onClick = { open = true }, modifier = Modifier.fillMaxWidth()) {
            Text("$label: ${value ?: "(default)"}")
        }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            if (allowNone) DropdownMenuItem(text = { Text("(default)") }, onClick = { onPick(null); open = false })
            options.forEach { option ->
                DropdownMenuItem(text = { Text(option) }, onClick = { onPick(option); open = false })
            }
        }
    }
}

// --- 1. connection ---

@Composable
fun ConnectionScreen(model: AppModel, modifier: Modifier) = Page(modifier) {
    Title("Connection")
    model.connectedTo?.let {
        val state = when (model.link) {
            ConnectionState.CONNECTED -> "Connected to"
            ConnectionState.RECONNECTING -> "Reconnecting to"
            ConnectionState.DISCONNECTED -> "Disconnected from"
        }
        Text(
            "$state $it",
            color = if (model.link == ConnectionState.DISCONNECTED) MaterialTheme.colorScheme.error
            else MaterialTheme.colorScheme.primary,
        )
    }
    val options = listOf(Settings.SSH to "SSH tunnel", Settings.DIRECT to "Direct URL (dev)")
    SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
        options.forEachIndexed { i, (value, label) ->
            SegmentedButton(
                selected = model.transport == value,
                onClick = { model.transport = value },
                shape = SegmentedButtonDefaults.itemShape(i, options.size),
            ) { Text(label) }
        }
    }
    if (model.transport == Settings.DIRECT) {
        OutlinedTextField(model.url, { model.url = it }, label = { Text("Broker URL") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Text(
            "For development only: plain HTTP, allowed only to 10.0.2.2 (the emulator's host) and 127.0.0.1.",
            style = MaterialTheme.typography.bodySmall,
        )
    } else {
        OutlinedTextField(model.host, { model.host = it }, label = { Text("SSH host") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedTextField(
                model.port, { model.port = it.filter(Char::isDigit) }, label = { Text("Port") }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number), modifier = Modifier.weight(1f),
            )
            OutlinedTextField(model.user, { model.user = it }, label = { Text("User") }, singleLine = true, modifier = Modifier.weight(2f))
        }
        OutlinedTextField(
            model.brokerPort, { model.brokerPort = it.filter(Char::isDigit) }, label = { Text("Broker port on the server") },
            singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number), modifier = Modifier.fillMaxWidth(),
            supportingText = { Text("den's broker listens on 127.0.0.1:${OpenSsh.BROKER_PORT} there unless configured otherwise") },
        )
        SshKeyCard(model)
    }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Button(onClick = model::connect, enabled = !model.connecting) { Text(if (model.connectedTo != null) "Reconnect" else "Connect") }
        if (model.connectedTo != null) OutlinedButton(onClick = model::disconnect) { Text("Disconnect") }
        if (model.connecting) CircularProgressIndicator()
    }
    model.hostKeyPrompt?.let { prompt ->
        Card {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(if (prompt.changed) "Host key changed!" else "New host", style = MaterialTheme.typography.titleMedium)
                Mono(prompt.fingerprint)
                if (prompt.changed) {
                    Text("The pinned key no longer matches. Forget it only if you changed the server's key yourself.")
                    OutlinedButton(onClick = model::forgetHostKey) { Text("Forget pinned key") }
                } else {
                    Text("Compare it with the server's (ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub there). Trusting pins it.")
                    Button(onClick = model::trustHostKey) { Text("Trust and connect") }
                }
            }
        }
    }
    if (model.hostKeyPrompt == null) ErrorText(model.connectError)
}

@Composable
private fun SshKeyCard(model: AppModel) {
    val clipboard = LocalClipboardManager.current
    Card {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("This device's key", style = MaterialTheme.typography.titleMedium)
            val key = model.publicKey
            if (key == null) {
                Text("The tunnel uses its own ed25519 key, made on this device and stored encrypted.")
                Button(onClick = model::createKey) { Text("Create key") }
            } else {
                Mono(OpenSsh.fingerprintOfLine(key))
                Text("Add this line to ~/.ssh/authorized_keys on the broker's machine; the key can then only forward to den's broker:")
                val line = OpenSsh.authorizedKeysLine(key, model.brokerPort.toIntOrNull() ?: OpenSsh.BROKER_PORT)
                Mono(line)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { clipboard.setText(AnnotatedString(line)) }) { Text("Copy line") }
                    TextButton(onClick = model::replaceKey) { Text("New key") }
                }
            }
        }
    }
}

// --- 2. status ---

@Composable
fun StatusScreen(model: AppModel, modifier: Modifier) = Page(modifier) {
    Title("Status")
    if (NotConnected(model)) return@Page
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Button(onClick = model::refreshStatus, enabled = !model.loadingStatus) { Text("Refresh") }
        if (model.loadingStatus) CircularProgressIndicator()
    }
    ErrorText(model.statusError)
    val s = model.status ?: return@Page
    val info = model.info
    val pressure = s.optJSONObject("pressure")
    val unavailable = s.optJSONObject("unavailable")
    val rows = listOf(
        "mode" to s.text("mode"),
        "model" to (s.textOrNull("llm_model") ?: "(none)"),
        "context" to s.opt("num_ctx")?.toString(),
        "loaded" to s.opt("loaded").let { if (it == null || it == JSONObject.NULL) "nothing" else it.toString() },
        "swapping" to s.opt("swapping").let { if (it == null || it == JSONObject.NULL) "no" else it.toString() },
        "in flight" to (s.optJSONArray("inflight")?.length() ?: 0).toString(),
        "waiting" to (s.optJSONArray("waiting")?.length() ?: 0).toString(),
        "pressure" to pressure?.let { "load ${it.opt("load_per_cpu")}/cpu, ${it.opt("free_ram_gb")} GB free" },
        "LLM" to unavailableText(unavailable?.opt("llm")),
        "images" to unavailableText(unavailable?.opt("image")),
    )
    rows.forEach { (k, v) -> if (v != null) Text("$k: $v") }
    if (info == null) return@Page
    HorizontalDivider()
    Text("Tasks", style = MaterialTheme.typography.titleMedium)
    val tasks = info.optJSONObject("tasks")
    if (tasks == null || tasks.length() == 0) Text("none enabled" + (info.optJSONObject("llm")?.opt("unavailable")?.takeIf { it != JSONObject.NULL }?.let { ": $it" } ?: ""))
    tasks?.keys()?.forEach { name ->
        Text("• $name: ${tasks.getJSONObject(name).optString("description").lineSequence().first()}")
    }
    HorizontalDivider()
    Text("Workflows", style = MaterialTheme.typography.titleMedium)
    val image = info.optJSONObject("image")
    val edits = image?.optJSONArray("edits")?.let { a -> (0 until a.length()).map { a.getString(it) } }.orEmpty()
    val workflows = image?.optJSONArray("workflows")?.let { a -> (0 until a.length()).map { a.getString(it) } }.orEmpty()
    image?.opt("unavailable")?.takeIf { it != JSONObject.NULL }?.let { Text("unavailable: $it") }
    workflows.forEach { w ->
        Text("• $w" + (if (w == image?.textOrNull("default")) " (default)" else "") + (if (w in edits) " [edits]" else ""))
    }
}

private fun unavailableText(value: Any?): String =
    if (value == null || value == JSONObject.NULL) "available" else "unavailable: $value"

// --- 3. ask ---

@Composable
fun AskScreen(model: AppModel, modifier: Modifier) = Page(modifier) {
    Title("Ask")
    if (NotConnected(model)) return@Page
    val tasks = model.info?.optJSONObject("tasks")
    val names = tasks?.keys()?.asSequence()?.toList().orEmpty()
    if (names.isEmpty()) {
        Text("This den has no tasks enabled.")
        return@Page
    }
    Picker("Task", model.task, names, { model.task = it })
    model.task?.let { t -> tasks?.optJSONObject(t)?.optString("description")?.let { Text(it, style = MaterialTheme.typography.bodySmall) } }
    OutlinedTextField(model.instructions, { model.instructions = it }, label = { Text("Instructions") }, modifier = Modifier.fillMaxWidth())
    OutlinedTextField(model.askText, { model.askText = it }, label = { Text("Text") }, minLines = 3, modifier = Modifier.fillMaxWidth())
    val pickFile = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) model.askFile = model.readPicked(uri)
    }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(onClick = { pickFile.launch(arrayOf("text/*", "application/json", "application/xml")) }) { Text("Attach file") }
        model.askFile?.let { f ->
            TextButton(onClick = { model.askFile = null }) { Text("${f.name} (${f.bytes.size} B) ✕") }
        }
    }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Button(onClick = model::ask, enabled = !model.asking && model.instructions.isNotBlank()) { Text("Ask") }
        if (model.asking) CircularProgressIndicator()
    }
    ErrorText(model.askError)
    val answer = model.answer ?: return@Page
    HorizontalDivider()
    SelectionContainer { Text(answer.optString("answer")) }
    answer.optJSONObject("stats")?.let { s ->
        Text(
            "${s.optString("model")} · ${s.opt("input_tokens")} in / ${s.opt("output_tokens")} out · ${s.opt("seconds")} s · id ${answer.opt("id")}",
            style = MaterialTheme.typography.bodySmall,
        )
    }
    Text("Verdict", style = MaterialTheme.typography.titleMedium)
    OutlinedTextField(model.feedbackNote, { model.feedbackNote = it }, label = { Text("Note (optional)") }, modifier = Modifier.fillMaxWidth())
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        listOf("ok", "partly", "wrong").forEach { v -> OutlinedButton(onClick = { model.sendFeedback(v) }) { Text(v) } }
    }
    model.feedbackSent?.let { Text(it) }
}

// --- 4. image ---

@Composable
fun ImageScreen(model: AppModel, modifier: Modifier) = Page(modifier) {
    Title(if (model.clipMode) "Clip" else "Image")
    if (NotConnected(model)) return@Page
    // An older den offers no clips; then this is only the image screen it always was.
    if (model.info?.optJSONObject("clip") != null) {
        SingleChoiceSegmentedButtonRow {
            listOf("Image", "Clip").forEachIndexed { i, label ->
                SegmentedButton(
                    selected = model.clipMode == (i == 1),
                    onClick = { model.clipMode = i == 1 },
                    shape = SegmentedButtonDefaults.itemShape(i, 2),
                ) { Text(label) }
            }
        }
        if (model.clipMode) {
            ClipPane(model)
            return@Page
        }
    }
    val image = model.info?.optJSONObject("image")
    image?.opt("unavailable")?.takeIf { it != JSONObject.NULL }?.let { ErrorText("Images unavailable: $it") }
    val all = image?.optJSONArray("workflows")?.let { a -> (0 until a.length()).map { a.getString(it) } }.orEmpty()
    val edits = image?.optJSONArray("edits")?.let { a -> (0 until a.length()).map { a.getString(it) } }.orEmpty()
    val choices = if (model.inputImage != null) edits else all
    OutlinedTextField(
        model.prompt, { model.prompt = it },
        label = { Text(if (model.inputImage != null) "Edit instruction" else "Prompt") },
        minLines = 3, modifier = Modifier.fillMaxWidth(),
    )
    Picker("Workflow", model.workflow, choices, { model.workflow = it }, allowNone = true)
    if (model.inputImage != null && model.workflow != null && model.workflow !in edits) {
        ErrorText("${model.workflow} can't edit; pick one of: ${edits.joinToString()}")
    }
    val pickImage = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
        if (uri != null) model.inputImage = model.readPicked(uri)
    }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(onClick = { pickImage.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)) }) {
            Text("Input image")
        }
        model.inputImage?.let { TextButton(onClick = { model.inputImage = null }) { Text("${it.name} ✕") } }
    }
    model.inputImage?.bitmap?.let {
        Image(it.asImageBitmap(), "input image", Modifier.heightIn(max = 120.dp), contentScale = ContentScale.Fit)
    }
    var more by remember { mutableStateOf(false) }
    TextButton(onClick = { more = !more }) { Text(if (more) "Fewer settings" else "More settings") }
    val fields = model.imageFields()
    val shown = if (more) fields else fields.filter { it.name in setOf("size", "steps", "seed") }
    shown.forEach { FieldInput(model, it) }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Button(onClick = model::generate, enabled = !model.generating && model.prompt.isNotBlank()) { Text("Generate") }
        if (model.generating) CircularProgressIndicator()
    }
    ErrorText(model.imageError)
    model.progress.forEach { Text(it, style = MaterialTheme.typography.bodySmall) }
    model.resultImages.forEach { Image(it.asImageBitmap(), "result", Modifier.fillMaxWidth(), contentScale = ContentScale.FillWidth) }
    model.resultMaps.forEach { (label, bitmap) ->
        Text(label, style = MaterialTheme.typography.bodySmall)
        Image(bitmap.asImageBitmap(), label, Modifier.heightIn(max = 160.dp), contentScale = ContentScale.Fit)
    }
    model.resultSummary?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
}

/** A short video clip (ADR clip-generation): it takes minutes and goes on while the app is away. */
@Composable
private fun ClipPane(model: AppModel) {
    val clip = model.info?.optJSONObject("clip") ?: return
    clip.opt("unavailable")?.takeIf { it != JSONObject.NULL }?.let { ErrorText("Clips unavailable: $it") }
    val flows = clip.optJSONArray("workflows")?.let { a -> (0 until a.length()).map { a.getString(it) } }.orEmpty()
    OutlinedTextField(
        model.clipPrompt, { model.clipPrompt = it },
        label = { Text("What happens in the clip") },
        minLines = 3, modifier = Modifier.fillMaxWidth(),
    )
    Picker("Workflow", model.clipWorkflow, flows, { model.clipWorkflow = it }, allowNone = true)
    OutlinedTextField(
        model.clipNegative, { model.clipNegative = it },
        label = { Text("Avoid (negative prompt)") },
        supportingText = { Text("Added to the workflow's own, e.g. extra limbs") },
        modifier = Modifier.fillMaxWidth(),
    )
    OutlinedTextField(
        model.clipDuration, { model.clipDuration = it },
        label = { Text("Seconds (default: the workflow's)") },
        singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
        modifier = Modifier.fillMaxWidth(),
    )
    OutlinedTextField(
        model.clipSize, { model.clipSize = it },
        label = { Text("Size, e.g. 704x1280") },
        supportingText = { Text("Default: the start frame's shape, or the workflow's without one") },
        singleLine = true,
        modifier = Modifier.fillMaxWidth(),
    )
    // Sound: only a workflow that makes it offers the switch; the prompt says what it sounds like.
    if (model.clipMakesSound()) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Switch(model.clipSound, { model.clipSound = it })
            Column(Modifier.weight(1f)) {
                Text("Sound")
                Text("Describe the sounds in the prompt", style = MaterialTheme.typography.bodySmall)
            }
        }
    } else {
        Text("Sound: none, this workflow makes silent clips", style = MaterialTheme.typography.bodySmall)
    }
    // A voice-over: spoken first, then mixed over the clip's sound (ADR voice-overs). Only where speech runs.
    if (model.voiceInfo() != null) {
        val pickScript = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            if (uri != null) model.readPicked(uri)?.let(model::loadScript)
        }
        var newVoice by remember { mutableStateOf("") }
        val askMic = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) model.toggleRecording(newVoice.trim()) else model.voiceNote = "recording needs the microphone permission"
        }
        val pickRecording = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            if (uri != null) model.readPicked(uri)?.let { model.addVoice(newVoice.trim(), it) }
        }
        OutlinedTextField(
            model.clipVoiceover, { model.clipVoiceover = it },
            label = { Text("Voice-over (optional)") },
            supportingText = { Text("A line to say, or an SRT script: each line at its time, and the clip lasts to its end") },
            minLines = 2, modifier = Modifier.fillMaxWidth(),
        )
        val askMicNarration = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) model.toggleNarration() else model.voiceNote = "recording needs the microphone permission"
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Button(
                onClick = { if (model.recordingNarration) model.toggleNarration() else askMicNarration.launch(Manifest.permission.RECORD_AUDIO) },
                enabled = !model.recording || model.recordingNarration,
            ) { Text(if (model.recordingNarration) "Stop" else "Record narration") }
            OutlinedButton(onClick = { pickScript.launch(arrayOf("application/x-subrip", "text/*", "*/*")) }) { Text("Load SRT") }
            if (model.clipVoiceover.isNotEmpty()) TextButton(onClick = { model.clipVoiceover = "" }) { Text("Clear") }
        }
        if (model.clipVoiceover.isNotBlank()) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.weight(1f)) {
                    Picker("Voice", model.clipVoice, model.voiceNames(), { model.clipVoice = it }, allowNone = true)
                }
                model.clipVoice?.let { name -> OutlinedButton(onClick = { model.listen(name) }) { Text("Listen") } }
            }
            Picker("Language", model.clipLanguage, model.languages(), { model.clipLanguage = it }, allowNone = true)
            if (model.narration != null) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Switch(model.useNarration, { model.useNarration = it })
                    Column(Modifier.weight(1f)) {
                        Text("Use my recording")
                        Text("Off: the voice below reads the script instead", style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
            if (model.clipCanLipSync()) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Switch(model.clipLipSync, { model.clipLipSync = it })
                    Column(Modifier.weight(1f)) {
                        Text("Lip-sync")
                        Text("A person on screen speaks it; say who in the prompt", style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        }
        model.lastSrt?.let {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton(onClick = { model.clipVoiceover = it }) { Text("Use the timed script") }
                TextButton(onClick = model::saveSrt) { Text("Save SRT") }
            }
        }
        // A new voice from a recording: about 10 seconds of clear speech, no music or echo.
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(
                newVoice, { newVoice = it.lowercase().replace(Regex("[^a-z0-9-]"), "-") },
                label = { Text("New voice name") }, singleLine = true, modifier = Modifier.weight(1f),
            )
            val sampling = model.recording && !model.recordingNarration
            Button(
                onClick = { if (sampling) model.toggleRecording(newVoice.trim()) else askMic.launch(Manifest.permission.RECORD_AUDIO) },
                enabled = newVoice.isNotBlank() && !model.recordingNarration,
            ) { Text(if (sampling) "Stop" else "Record") }
            OutlinedButton(onClick = { pickRecording.launch(arrayOf("audio/*")) }, enabled = newVoice.isNotBlank() && !model.recording) {
                Text("File")
            }
        }
        // Or describe one: the den designs a sample in that voice and keeps it (ADR voice-overs).
        if (model.canDesignVoices()) {
            var described by remember { mutableStateOf("") }
            OutlinedTextField(
                described, { described = it },
                label = { Text("Or describe the new voice") },
                supportingText = { Text("e.g. an old man with a deep, raspy, slow voice") },
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedButton(
                onClick = { model.designVoice(described.trim()) },
                enabled = described.isNotBlank() && !model.recording,
            ) { Text("Design voice") }
            // A designed voice is a draft until it's heard and saved, like a pose before it's kept.
            model.voiceDraft?.let {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                    TextButton(onClick = model::listenDraft) { Text("Listen") }
                    TextButton(onClick = { model.tryDraft(model.clipVoiceover.lines().firstOrNull { l -> l.isNotBlank() && "-->" !in l && l.trim().toIntOrNull() == null } ?: "Every morning I walk down to the harbour and watch the boats come in.") }) { Text("Try a line") }
                    Button(onClick = { model.saveDraft(newVoice.trim()) }, enabled = newVoice.isNotBlank()) {
                        Text(if (newVoice.isBlank()) "Save (name it above)" else "Save as ${newVoice.trim()}")
                    }
                }
            }
        }
        model.voiceNote?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
    }
    // LoRAs: the ones this workflow's model takes, each switched on with an optional strength.
    val loraChoices = model.clipLoraChoices()
    if (loraChoices.isEmpty()) {
        Text("LoRAs: none installed for this workflow's model", style = MaterialTheme.typography.bodySmall)
    }
    loraChoices.forEach { (name, choice) ->
        val (description, default) = choice
        val picked = name in model.clipLoras
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Switch(picked, { on -> if (on) model.clipLoras[name] = "" else model.clipLoras.remove(name) })
            Column(Modifier.weight(1f)) {
                Text("LoRA $name")
                if (description.isNotEmpty()) Text(description, style = MaterialTheme.typography.bodySmall)
            }
            if (picked) {
                OutlinedTextField(
                    model.clipLoras[name].orEmpty(), { model.clipLoras[name] = it },
                    label = { Text("Strength") },
                    placeholder = { Text("$default") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                    modifier = Modifier.width(112.dp),
                )
            }
        }
    }
    // Keyframes: pictures the clip must show, each at its moment. The workflow says how many.
    val limit = model.clipKeyframeLimit()
    val pickFrame = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
        if (uri != null) model.readPicked(uri)?.let(model::addClipKeyframe)
    }
    model.clipKeyframes.forEachIndexed { i, keyframe ->
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            keyframe.picked.bitmap?.let {
                Image(it.asImageBitmap(), "keyframe ${i + 1}", Modifier.heightIn(max = 72.dp).width(128.dp), contentScale = ContentScale.Fit)
            }
            OutlinedTextField(
                keyframe.at, { keyframe.at = it },
                label = { Text("At") },
                supportingText = { Text("start, end, 2.5 or 50%") },
                singleLine = true,
                modifier = Modifier.weight(1f),
            )
            TextButton(onClick = { model.clipKeyframes.removeAt(i) }) { Text("✕") }
        }
    }
    if (model.clipKeyframes.size > limit) {
        ErrorText("This workflow takes ${if (limit == 0) "no keyframes" else "up to $limit"}; remove ${model.clipKeyframes.size - limit}")
    }
    if (limit > 0) {
        OutlinedButton(
            onClick = { pickFrame.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)) },
            enabled = model.clipKeyframes.size < limit,
        ) {
            Text(if (model.clipKeyframes.isEmpty()) "Start frame" else "Add keyframe (${model.clipKeyframes.size} of $limit)")
        }
    }
    Text("A clip takes minutes and goes on if you leave the app.", style = MaterialTheme.typography.bodySmall)
    val busy = model.clipId != null || model.clipState == "starting"
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
        Button(onClick = model::startClip, enabled = !busy && model.clipPrompt.isNotBlank()) { Text("Make clip") }
        if (busy) {
            CircularProgressIndicator()
            TextButton(onClick = model::cancelClip, enabled = model.clipId != null) { Text("Cancel") }
        }
    }
    ErrorText(model.clipError)
    model.clipState?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
    // Four frames, first to last: what the clip did, before playing it.
    model.clipSheet?.let { Image(it.asImageBitmap(), "contact sheet", Modifier.fillMaxWidth(), contentScale = ContentScale.FillWidth) }
    model.clipUri?.let { uri ->
        val context = LocalContext.current
        Button(onClick = {
            runCatching {
                context.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "video/*").addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION))
            }.onFailure { model.clipError = "no app here plays videos" }
        }) { Text("Play") }
    }
    model.clipSummary?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
}

@Composable
private fun FieldInput(model: AppModel, field: Field) {
    val value = model.imageValues[field.name].orEmpty()
    when {
        field.choices.isNotEmpty() -> Picker(field.name, value.ifEmpty { null }, field.choices, { v ->
            if (v == null) model.imageValues.remove(field.name) else model.imageValues[field.name] = v
        }, allowNone = true)
        field.type == "boolean" -> Row {
            Text(field.name, Modifier.weight(1f).padding(top = 12.dp))
            Switch(checked = value == "true", onCheckedChange = { model.imageValues[field.name] = it.toString() })
        }
        else -> OutlinedTextField(
            value, { model.imageValues[field.name] = it },
            label = { Text(field.name) },
            placeholder = { Text(field.description.take(60), style = MaterialTheme.typography.bodySmall) },
            singleLine = field.type != "string" || field.name != "negative",
            keyboardOptions = KeyboardOptions(
                keyboardType = if (field.type == "integer" || field.type == "number") KeyboardType.Number else KeyboardType.Text,
            ),
            modifier = Modifier.fillMaxWidth(),
        )
    }
}

/** A Delete button that asks first: a pose or a voice is gone for every client of this den. */
@Composable
private fun ConfirmedDelete(kind: String, name: String, delete: () -> Unit) {
    var asking by remember(name) { mutableStateOf(false) }
    OutlinedButton(onClick = { asking = true }) { Text("Delete") }
    if (asking) {
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { asking = false },
            title = { Text("Delete the $kind $name?") },
            text = { Text("It's removed from the den's library, for every client. This can't be undone.") },
            confirmButton = { TextButton(onClick = { asking = false; delete() }) { Text("Delete") } },
            dismissButton = { TextButton(onClick = { asking = false }) { Text("Cancel") } },
        )
    }
}

// --- 6. voices ---

/** The den's voice library: each voice with what it is, to listen to or delete (ADR voice-overs). */
@Composable
fun VoicesScreen(model: AppModel, modifier: Modifier) = Page(modifier) {
    Title("Voices")
    if (NotConnected(model)) return@Page
    val voice = model.voiceInfo()
    if (voice == null) {
        Text("This den has no speech installed.")
        return@Page
    }
    model.voiceNote?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
    val toc = voice.optJSONArray("toc")
    if (toc == null || toc.length() == 0) {
        Text("No voices yet: design one or record one on the Image screen's Clip tab.")
        return@Page
    }
    for (i in 0 until toc.length()) {
        val row = toc.getJSONObject(i)
        val name = row.getString("name")
        Text(name + (row.optString("source").takeIf { it.isNotEmpty() }?.let { " · $it" } ?: ""), style = MaterialTheme.typography.titleSmall)
        row.optString("description").takeIf { it.isNotEmpty() && it != "null" }?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = { model.listen(name) }) { Text("Listen") }
            ConfirmedDelete("voice", name) { model.deleteVoice(name) }
        }
        HorizontalDivider()
    }
}

// --- 5. poses ---

@Composable
fun PosesScreen(model: AppModel, modifier: Modifier) = Page(modifier) {
    // The title row carries the refresh, as an icon at the right; while the list loads the
    // spinner takes the icon's place, so the row never jumps.
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
        Title("Poses")
        if (model.loadingPoses) {
            CircularProgressIndicator(Modifier.size(24.dp), strokeWidth = 2.dp)
        } else {
            IconButton(onClick = model::loadPoses, enabled = model.client != null) {
                Icon(Icons.Default.Refresh, contentDescription = "Refresh the list of poses")
            }
        }
    }
    if (NotConnected(model)) return@Page
    LaunchedEffect(model.connectedTo) { if (model.poseToc == null) model.loadPoses() }
    ErrorText(model.posesError)
    val selected = model.pose
    if (selected != null) {
        TextButton(onClick = { model.openPose(null) }) { Text("← all poses") }
        // Swipe left for the next pose, right for the previous one; a small drag does nothing.
        val threshold = with(LocalDensity.current) { 80.dp.toPx() }
        Column(
            Modifier.fillMaxWidth().pointerInput(selected) {
                var dragged = 0f
                detectHorizontalDragGestures(
                    onDragStart = { dragged = 0f },
                    onDragEnd = {
                        if (dragged < -threshold) model.stepPose(+1) else if (dragged > threshold) model.stepPose(-1)
                    },
                ) { _, dx -> dragged += dx }
            },
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(selected, style = MaterialTheme.typography.titleMedium)
            model.posePosition()?.let { (at, of) ->
                Text("$at of $of · swipe for the next or previous", style = MaterialTheme.typography.bodySmall)
            }
            // The source photo on top; the button at its bottom right switches to the skeleton and back.
            val shown = if (model.showSkeleton || model.posePhoto == null) model.poseSkeleton else model.posePhoto
            shown?.let { bitmap ->
                Box(Modifier.fillMaxWidth()) {
                    Image(bitmap.asImageBitmap(), selected, Modifier.fillMaxWidth(), contentScale = ContentScale.FillWidth)
                    if (model.posePhoto != null && model.poseSkeleton != null) {
                        androidx.compose.material3.SmallFloatingActionButton(
                            onClick = { model.showSkeleton = !model.showSkeleton },
                            modifier = Modifier.align(Alignment.BottomEnd).padding(12.dp),
                        ) { Text(if (model.showSkeleton) "Photo" else "Skeleton", Modifier.padding(horizontal = 12.dp)) }
                    }
                }
            }
            model.poseDetails?.let { SelectionContainer { Text(it, style = MaterialTheme.typography.bodySmall) } }
        }
        ConfirmedDelete("pose", selected) { model.deletePose(selected) }
        return@Page
    }
    // Making one is its own flow, in NewPose.kt; a saved pose shows up in the list below.
    NewPose(model, onSaved = model::loadPoses)
    HorizontalDivider()
    if (model.poseNames.isEmpty()) model.poseToc?.let { Text(it) }
    model.poseNames.forEach { name ->
        Text(name, Modifier.fillMaxWidth().clickable { model.openPose(name) }.padding(vertical = 8.dp))
        HorizontalDivider()
    }
}
