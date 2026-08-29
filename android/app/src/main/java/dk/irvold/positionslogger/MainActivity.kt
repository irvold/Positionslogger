package dk.irvold.positionslogger

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlin.math.roundToInt

private val GREEN = Color(0xFF2E7D32)
private val RED = Color(0xFFC62828)
private val GREY = Color(0xFF9E9E9E)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = if (isSystemInDarkTheme()) darkColorScheme() else lightColorScheme()) {
                Screen()
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun Screen() {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val snackbar = remember { SnackbarHostState() }

    val stored: Settings? by Prefs.flow(ctx)
        .collectAsStateWithLifecycle<Settings?>(initialValue = null)
    var form by remember { mutableStateOf<Settings?>(null) }
    LaunchedEffect(stored) { if (form == null && stored != null) form = stored }

    var queued by remember { mutableIntStateOf(0) }
    var vpnActive by remember { mutableStateOf(false) }
    var probe by remember { mutableStateOf<Net.Probe?>(null) }
    var showConnDialog by remember { mutableStateOf(false) }

    // Opdaterer kø-tal, VPN-status og server-status løbende
    LaunchedEffect(Unit) {
        while (true) {
            queued = withContext(Dispatchers.IO) { QueueDb.get(ctx).count() }
            vpnActive = Net.isVpnActive(ctx)
            probe = if (form?.isComplete() == true) Net.probe(ctx) else Net.Probe.NoConfig
            delay(6000)
        }
    }
    // Hurtig gen-test når server-URL eller token ændres
    LaunchedEffect(form?.serverUrl, form?.token) {
        delay(700)
        if (form?.isComplete() == true) probe = Net.probe(ctx)
    }

    val running = stored?.wantRunning == true

    fun persist(newForm: Settings) {
        form = newForm
        scope.launch { Prefs.save(ctx, newForm) }
    }

    fun startLogging() {
        scope.launch { Prefs.setRunning(ctx, true) }
        LocationService.start(ctx)
    }

    val bgPermLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { startLogging() }

    val fgPermLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { result ->
        val fine = result[Manifest.permission.ACCESS_FINE_LOCATION] == true
        val coarse = result[Manifest.permission.ACCESS_COARSE_LOCATION] == true
        if (!fine && !coarse) {
            scope.launch { snackbar.showSnackbar("Placeringstilladelse blev afvist") }
            return@rememberLauncherForActivityResult
        }
        val needsBg = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_BACKGROUND_LOCATION) !=
            PackageManager.PERMISSION_GRANTED
        if (needsBg) bgPermLauncher.launch(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
        else startLogging()
    }

    fun launchPermsAndStart() {
        val perms = buildList {
            add(Manifest.permission.ACCESS_FINE_LOCATION)
            add(Manifest.permission.ACCESS_COARSE_LOCATION)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU)
                add(Manifest.permission.POST_NOTIFICATIONS)
        }
        fgPermLauncher.launch(perms.toTypedArray())
    }

    fun onStart() {
        val f = form ?: return
        if (!f.isComplete()) {
            scope.launch { snackbar.showSnackbar("Udfyld server-URL, enheds-ID og token først") }
            return
        }
        scope.launch {
            val p = Net.probe(ctx)
            probe = p
            if (p is Net.Probe.Unreachable) showConnDialog = true
            else launchPermsAndStart()
        }
    }

    fun onStop() {
        scope.launch { Prefs.setRunning(ctx, false) }
        LocationService.stop(ctx)
    }

    Scaffold(
        topBar = { TopAppBar(title = { Text("Positionslogger") }) },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { pad ->
        val f = form
        if (f == null) {
            Column(Modifier.padding(pad).padding(24.dp)) { Text("Indlæser…") }
            return@Scaffold
        }

        Column(
            Modifier
                .padding(pad)
                .padding(16.dp)
                .fillMaxSize()
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            StatusCard(running = running, queued = queued, vpnActive = vpnActive, probe = probe)

            if (probe is Net.Probe.Unreachable) {
                OutlinedButton(
                    onClick = { Net.openTailscale(ctx) },
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("Åbn Tailscale") }
            }

            OutlinedTextField(
                value = f.serverUrl,
                onValueChange = { persist(f.copy(serverUrl = it.trim())) },
                label = { Text("Server-URL (fx http://raspberrypi:5000/position)") },
                singleLine = true,
                enabled = !running,
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = f.deviceId,
                onValueChange = { persist(f.copy(deviceId = it.trim())) },
                label = { Text("Enheds-ID (fx daniels-telefon)") },
                singleLine = true,
                enabled = !running,
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = f.token,
                onValueChange = { persist(f.copy(token = it.trim())) },
                label = { Text("Ingest-token") },
                singleLine = true,
                enabled = !running,
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = KeyboardType.Password),
                modifier = Modifier.fillMaxWidth(),
            )

            SliderRow(
                title = "Interval mellem målinger",
                options = listOf(5, 10, 30, 60, 120, 300),
                selected = f.intervalSec,
                format = { if (it < 60) "$it s" else "${it / 60} min" },
                enabled = !running,
                onSelect = { persist(f.copy(intervalSec = it)) },
            )

            SliderRow(
                title = "Mindste afstand mellem punkter",
                options = listOf(0, 10, 25, 50, 100),
                selected = f.minDistanceM,
                format = { if (it == 0) "fra" else "$it m" },
                enabled = !running,
                onSelect = { persist(f.copy(minDistanceM = it)) },
            )

            SliderRow(
                title = "Maks. unøjagtighed (kassér skrald)",
                options = listOf(0, 15, 25, 50, 100),
                selected = f.maxAccuracyM,
                format = { if (it == 0) "fra" else "$it m" },
                enabled = !running,
                onSelect = { persist(f.copy(maxAccuracyM = it)) },
            )

            ToggleRow("Høj nøjagtighed (GPS)", f.highAccuracy, enabled = !running) {
                persist(f.copy(highAccuracy = it))
            }
            ToggleRow("Start automatisk efter genstart", f.startOnBoot, enabled = true) {
                persist(f.copy(startOnBoot = it))
            }

            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                if (running) {
                    Button(onClick = { onStop() }, modifier = Modifier.weight(1f)) {
                        Text("Stop logning", maxLines = 1)
                    }
                } else {
                    Button(onClick = { onStart() }, modifier = Modifier.weight(1f)) {
                        Text("Start logning", maxLines = 1)
                    }
                }
                OutlinedButton(
                    onClick = {
                        scope.launch { snackbar.showSnackbar("Kø: ${Uploader.flush(ctx)}") }
                    },
                    modifier = Modifier.weight(1f),
                ) { Text("Send kø ($queued)", maxLines = 1) }
            }

            Text(
                "Tip: giv \"Tillad hele tiden\" + fjern batterioptimering for stabil baggrundslogning.",
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }

    if (showConnDialog) {
        val host = form?.serverUrl.orEmpty()
        AlertDialog(
            onDismissRequest = { showConnDialog = false },
            title = { Text("Kan ikke nå serveren") },
            text = {
                Text(
                    "Serveren på\n$host\nsvarer ikke.\n\n" +
                        if (vpnActive) "VPN er aktiv – tjek at serveren kører."
                        else "Tailscale/VPN ser ud til at være slukket."
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    showConnDialog = false
                    Net.openTailscale(ctx)
                }) { Text("Åbn Tailscale") }
            },
            dismissButton = {
                Row {
                    TextButton(onClick = {
                        scope.launch {
                            val p = Net.probe(ctx)
                            probe = p
                            if (p !is Net.Probe.Unreachable) {
                                showConnDialog = false
                                launchPermsAndStart()
                            }
                        }
                    }) { Text("Prøv igen") }
                    TextButton(onClick = {
                        showConnDialog = false
                        launchPermsAndStart()
                    }) { Text("Start alligevel") }
                }
            },
        )
    }
}

@Composable
private fun StatusCard(running: Boolean, queued: Int, vpnActive: Boolean, probe: Net.Probe?) {
    val (serverText, serverColor) = when (probe) {
        Net.Probe.Reachable -> "OK" to GREEN
        is Net.Probe.HttpError -> "HTTP ${probe.code}" to GREEN
        is Net.Probe.Unreachable -> "svarer ikke" to RED
        Net.Probe.NoConfig -> "–" to GREY
        null -> "…" to GREY
    }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(
                if (running) "● Logger position" else "○ Stoppet",
                style = MaterialTheme.typography.titleMedium,
            )
            Row(
                verticalAlignment = androidx.compose.ui.Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(16.dp),
            ) {
                DotText("Tailscale " + if (vpnActive) "aktiv" else "inaktiv", if (vpnActive) GREEN else GREY)
                DotText("Server $serverText", serverColor)
            }
        }
    }
}

@Composable
private fun DotText(text: String, color: Color) {
    Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
        Text("● ", color = color, style = MaterialTheme.typography.bodyMedium)
        Text(text, style = MaterialTheme.typography.bodyMedium, maxLines = 1)
    }
}

/** Slider der snapper til præcis de værdier der står i [options] (samme trin som chips før). */
@Composable
private fun SliderRow(
    title: String,
    options: List<Int>,
    selected: Int,
    format: (Int) -> String,
    enabled: Boolean,
    onSelect: (Int) -> Unit,
) {
    val idx = options.indexOf(selected).let { if (it < 0) 0 else it }
    Column {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text(title, style = MaterialTheme.typography.labelLarge)
            Text(format(selected), style = MaterialTheme.typography.labelLarge)
        }
        Slider(
            value = idx.toFloat(),
            onValueChange = { v ->
                if (enabled) onSelect(options[v.roundToInt().coerceIn(0, options.lastIndex)])
            },
            valueRange = 0f..options.lastIndex.toFloat(),
            steps = (options.size - 2).coerceAtLeast(0),
            enabled = enabled,
        )
    }
}

@Composable
private fun ToggleRow(text: String, checked: Boolean, enabled: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(text)
        Switch(checked = checked, onCheckedChange = onChange, enabled = enabled)
    }
}
