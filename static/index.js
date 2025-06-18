// --- DOM Elements ---
// Header and Auth
const loggedInUsernameDisplay = document.getElementById('loggedInUsername');
const discordLoginBtn = document.querySelector('a[href*="login/discord"]');
const spotifyConnectBtn = document.querySelector('a[href*="login/spotify"]'); // Will be updated dynamically

// Navigation
const navButtons = document.querySelectorAll('.nav-tabs button');
const tabPanes = document.querySelectorAll('.tab-pane');

// Music Player Elements
const musicSearchInput = document.getElementById('musicSearchInput');
const searchMusicForm = document.getElementById('searchMusicForm');
const searchResultsList = document.getElementById('searchResultsList');
const nowPlayingContent = document.getElementById('nowPlayingContent');
const progressBar = document.getElementById('progressBar');
const currentTimeDisplay = document.getElementById('currentTime');
const totalTimeDisplay = document.getElementById('totalTime');
const shuffleBtn = document.getElementById('shuffleBtn');
const prevBtn = document.getElementById('prevBtn');
const playPauseBtn = document.getElementById('playPauseBtn');
const playIcon = document.getElementById('playIcon');
const pauseIcon = document.getElementById('pauseIcon');
const nextBtn = document.getElementById('nextBtn');
const repeatBtn = document.getElementById('repeatBtn');
const volumeSlider = document.getElementById('volumeSlider');
const addQueueItemBtn = document.getElementById('addQueueItemBtn');
const queueList = document.getElementById('queueList');

// AI Chat Elements
const chatHistoryElement = document.getElementById('chatHistory');
const chatInput = document.getElementById('chatInput');
const chatForm = document.getElementById('chatForm');
const thinkingIndicator = document.getElementById('thinkingIndicator');

// Reminders Elements
const reminderTextInput = document.getElementById('reminderTextInput');
const reminderTimeInput = document.getElementById('reminderTimeInput');
const reminderForm = document.getElementById('reminderForm');
const remindersList = document.getElementById('remindersList');

// Security Alerts Elements
const securityAlertsList = document.getElementById('securityAlertsList');

// Flash Messages Container
const flashMessagesContainer = document.getElementById('flashMessages');


// --- Global State Variables (matching React component structure) ---
let firebaseApp;
let firestoreDb;
let firebaseAuth;
let userId = null; // Will be set after Firebase auth
let isAuthReady = false; // Flag for Firebase auth readiness

let currentActiveTab = 'musicPlayer'; // Current active tab

let searchQuery = '';
let searchResults = []; // { title, artist, albumArt }
let currentSong = null; // { title, artist, albumArt }
let isPlaying = false;
let queue = []; // { title, artist, albumArt }
let volume = 70; // 0-100
let shuffleMode = false;
let repeatMode = false;

let chatHistory = [{ role: 'bot', text: 'Hello! How can I help you today?' }];
let isThinking = false;

let reminderText = '';
let reminderTime = '09:00';
let reminders = []; // { id, text, time, createdAt }

let safetyAlerts = []; // { id, type, message, timestamp }


// --- Firebase Initialization and Authentication ---
document.addEventListener('DOMContentLoaded', async () => {
    try {
        firebaseApp = firebase.initializeApp(FIREBASE_CONFIG);
        firestoreDb = firebase.firestore();
        firebaseAuth = firebase.auth();

        // Sign in anonymously or with custom token
        if (typeof INITIAL_AUTH_TOKEN !== 'undefined' && INITIAL_AUTH_TOKEN) {
            await firebaseAuth.signInWithCustomToken(INITIAL_AUTH_TOKEN);
            console.log("Signed in with custom token.");
        } else {
            await firebaseAuth.signInAnonymously();
            console.log("Signed in anonymously.");
        }

        // Listen for auth state changes
        firebaseAuth.onAuthStateChanged((user) => {
            if (user) {
                userId = user.uid;
                isAuthReady = true;
                console.log("Auth state changed, user ID:", userId);
                setupFirestoreListeners(); // Setup listeners after auth is ready
            } else {
                userId = null;
                isAuthReady = true;
                console.log("Auth state changed, no user.");
                // Optionally clear UI data if user logs out/is not authenticated
                clearUIState();
            }
            updateAuthUI(); // Update UI after auth state changes
        });

        // Initialize UI based on initial Flask-rendered data
        updateAuthUI();
        updateTabContent();
        updateMusicPlayerUI();
        updateQueueUI();
        updateChatUI();
        updateRemindersUI();
        updateSafetyAlertsUI();

        // Set up initial volume slider value
        volumeSlider.value = volume;

        // Auto-hide Flask flash messages (if any rendered by Jinja)
        document.querySelectorAll('.flash-message').forEach(flashDiv => {
            setTimeout(() => {
                flashDiv.style.animation = 'fadeOut 0.5s ease-out forwards';
                flashDiv.addEventListener('animationend', () => flashDiv.remove());
            }, 5000); // Message stays for 5 seconds
        });

    } catch (error) {
        console.error("Firebase initialization or authentication error:", error);
        showFlashMessage("Failed to initialize the application. Please try again later.", "error");
    }
});

// --- Firestore Data Subscriptions ---
function setupFirestoreListeners() {
    if (!firestoreDb || !userId || !isAuthReady) {
        console.log("Firestore not ready or user ID not available. Skipping data subscription setup.");
        return;
    }

    // Reference to the user's private data collection for queue and reminders
    const userMusicDataRef = firestoreDb.collection(`artifacts/${APP_ID}/users/${userId}/musicBotData`).doc('musicState');
    const userRemindersColRef = firestoreDb.collection(`artifacts/${APP_ID}/users/${userId}/reminders`);

    // Subscribe to music state updates (current song, queue, isPlaying, volume, shuffle, repeat)
    userMusicDataRef.onSnapshot((docSnap) => {
        if (docSnap.exists) {
            const data = docSnap.data();
            queue = data.queue || [];
            currentSong = data.currentSong || null;
            isPlaying = data.isPlaying || false;
            volume = data.volume !== undefined ? data.volume : 70;
            shuffleMode = data.shuffleMode || false;
            repeatMode = data.repeatMode || false;
            console.log("Music state data updated:", data);
        } else {
            console.log("No music state data found, initializing empty.");
            queue = [];
            currentSong = null;
            isPlaying = false;
            volume = 70;
            shuffleMode = false;
            repeatMode = false;
        }
        updateMusicPlayerUI(); // Update UI whenever music state changes
        updateQueueUI(); // Update queue UI
    }, (error) => {
        console.error("Error fetching music state:", error);
    });

    // Subscribe to reminders updates
    userRemindersColRef.onSnapshot((snapshot) => {
        reminders = snapshot.docs.map(doc => ({ id: doc.id, ...doc.data() }));
        console.log("Reminders updated:", reminders);
        updateRemindersUI(); // Update UI whenever reminders change
    }, (error) => {
        console.error("Error fetching reminders:", error);
    });
}

// Clear all relevant UI state when user is not logged in or auth fails
function clearUIState() {
    queue = [];
    currentSong = null;
    isPlaying = false;
    volume = 70;
    shuffleMode = false;
    repeatMode = false;
    chatHistory = [{ role: 'bot', text: 'Hello! How can I help you today?' }];
    reminders = [];
    safetyAlerts = [];
    searchResults = [];
    searchQuery = '';

    updateMusicPlayerUI();
    updateQueueUI();
    updateChatUI();
    updateRemindersUI();
    updateSafetyAlertsUI();
}


// --- UI Update Functions ---

function updateAuthUI() {
    if (discordLoginBtn) {
        discordLoginBtn.style.display = isDiscordLinked ? 'none' : 'inline-flex';
    }
    if (spotifyConnectBtn) {
        // Update Spotify connect link with current discordUserId
        if (discordUserId) {
            spotifyConnectBtn.href = `${BASE_URL}login/spotify/${discordUserId}`;
        }
        spotifyConnectBtn.style.display = (isDiscordLinked && !isSpotifyLinked) ? 'inline-flex' : 'none';
    }

    if (loggedInUsernameDisplay) {
        loggedInUsernameDisplay.textContent = discordUsername || (discordUserId ? `User ID: ${discordUserId}` : 'Guest User');
    }

    // Enable/disable music player controls based on linking status
    const controlsEnabled = isDiscordLinked; // Primary condition for control visibility
    musicSearchInput.disabled = !isSpotifyLinked; // Search only if Spotify linked
    searchMusicForm.querySelector('button[type="submit"]').disabled = !isSpotifyLinked;

    playPauseBtn.disabled = !controlsEnabled;
    prevBtn.disabled = !controlsEnabled;
    nextBtn.disabled = !controlsEnabled;
    shuffleBtn.disabled = !controlsEnabled;
    repeatBtn.disabled = !controlsEnabled;
    volumeSlider.disabled = !controlsEnabled;
    addQueueItemBtn.disabled = !controlsEnabled;

    // Additional styling for shuffle/repeat buttons
    if (shuffleBtn) {
        shuffleBtn.classList.toggle('active', shuffleMode);
    }
    if (repeatBtn) {
        repeatBtn.classList.toggle('active', repeatMode);
    }
}

function updateTabContent() {
    tabPanes.forEach(pane => {
        pane.style.display = 'none';
    });
    document.getElementById(`${currentActiveTab}Content`).style.display = 'block';

    navButtons.forEach(button => {
        button.classList.remove('active');
        if (button.dataset.tab === currentActiveTab) {
            button.classList.add('active');
        }
    });
}

function updateMusicPlayerUI() {
    // Now Playing section
    nowPlayingContent.innerHTML = ''; // Clear previous content
    if (currentSong) {
        nowPlayingContent.innerHTML = `
            <img src="${currentSong.albumArt || `https://placehold.co/150x150/4A148C/FFFFFF?text=🎵`}" alt="Album Art" class="w-40 h-40 rounded-xl shadow-lg mb-6" />
            <p class="text-2xl font-bold text-white mb-1">${currentSong.title}</p>
            <p class="text-lg text-gray-300 mb-4">${currentSong.artist}</p>
        `;
    } else {
        nowPlayingContent.innerHTML = `
            <div class="now-playing-placeholder">
                <span class="icon">🎵</span>
                <h3>Welcome to your Music Bot</h3>
                <p>Get started by searching for a song</p>
            </div>
        `;
    }

    // Play/Pause button icon
    if (isPlaying) {
        playIcon.style.display = 'none';
        pauseIcon.style.display = 'inline-block';
        playPauseBtn.classList.add('active'); // Add active state for styling
    } else {
        playIcon.style.display = 'inline-block';
        pauseIcon.style.display = 'none';
        playPauseBtn.classList.remove('active'); // Remove active state
    }

    // Volume slider
    volumeSlider.value = volume;

    // Progress bar (simulated for now, would be based on actual playback)
    // For now, let's keep it static or remove if not tied to backend progress
    // If Flask API sends progress, update it here:
    // progressBar.style.width = `${(currentTrackProgress / currentTrackDuration) * 100}%`;
    // currentTimeDisplay.textContent = formatTime(currentTrackProgress);
    // totalTimeDisplay.textContent = formatTime(currentTrackDuration);
}

function updateQueueUI() {
    queueList.innerHTML = ''; // Clear existing items

    if (queue.length > 0) {
        queue.forEach((song, index) => {
            const listItem = document.createElement('li');
            listItem.innerHTML = `
                <span class="queue-index">${index + 1}.</span>
                <img src="${song.albumArt || `https://placehold.co/40x40/4A148C/FFFFFF?text=🎵`}" alt="Album Art" class="w-10 h-10 rounded-md mr-3 flex-shrink-0" />
                <div class="song-info">
                    <p class="title">${song.title}</p>
                    <p class="artist">${song.artist}</p>
                </div>
                <button class="btn-small-icon red remove-btn" data-index="${index}" title="Remove from Queue">
                    <i class="fas fa-times"></i>
                </button>
            `;
            queueList.appendChild(listItem);
        });
    } else {
        queueList.innerHTML = `<li class="text-center text-gray-400 italic" style="margin-top: 10px;">Queue is empty. Search for songs to add!</li>`;
    }
}

function updateSearchResultsUI() {
    searchResultsList.innerHTML = ''; // Clear previous results

    if (searchResults.length > 0) {
        searchResults.forEach((song, index) => {
            const listItem = document.createElement('li');
            listItem.innerHTML = `
                <img src="${song.albumArt}" alt="Album Art" />
                <div class="song-info">
                    <p class="title">${song.title}</p>
                    <p class="artist">${song.artist}</p>
                </div>
                <button class="btn-small-icon btn-green add-to-queue-btn" data-index="${index}" title="Add to Queue">
                    <i class="fas fa-plus"></i>
                </button>
            `;
            searchResultsList.appendChild(listItem);
        });
    }
}

function updateChatUI() {
    chatHistoryElement.innerHTML = ''; // Clear existing
    chatHistory.forEach(msg => {
        const messageDiv = document.createElement('div');
        messageDiv.className = `chat-message ${msg.role}`;
        messageDiv.innerHTML = `
            <div class="avatar">${msg.role === 'user' ? '👤' : '🤖'}</div>
            <div class="message-content">${msg.text}</div>
        `;
        chatHistoryElement.appendChild(messageDiv);
    });
    // Scroll to bottom
    chatHistoryElement.scrollTop = chatHistoryElement.scrollHeight;
    thinkingIndicator.style.display = isThinking ? 'block' : 'none';
}

function updateRemindersUI() {
    remindersList.innerHTML = ''; // Clear existing

    if (reminders.length > 0) {
        reminders.forEach((r) => {
            const listItem = document.createElement('li');
            listItem.innerHTML = `
                <div class="reminder-info">
                    <span class="reminder-text">${r.text}</span>
                    <span class="reminder-time"> at ${r.time}</span>
                </div>
                <button class="delete-btn" data-id="${r.id}">Delete</button>
            `;
            remindersList.appendChild(listItem);
        });
    } else {
        remindersList.innerHTML = `<li class="text-center text-gray-400 italic" style="margin-top: 10px;">No reminders set.</li>`;
    }
}

function updateSafetyAlertsUI() {
    securityAlertsList.innerHTML = ''; // Clear existing

    if (safetyAlerts.length > 0) {
        safetyAlerts.forEach(alert => {
            const listItem = document.createElement('li');
            listItem.innerHTML = `
                <span class="alert-type">${alert.type}:</span>
                <span class="alert-message">${alert.message}</span>
                <span class="alert-timestamp">${alert.timestamp}</span>
            `;
            securityAlertsList.appendChild(listItem);
        });
    } else {
        securityAlertsList.innerHTML = `<li class="text-center text-gray-400 italic" style="margin-top: 10px;">No active security alerts.</li>`;
    }
}

// --- Firebase Music State Persistence Helpers ---
async function updateMusicStateInFirestore() {
    if (!firestoreDb || !userId) {
        console.error("Firestore not initialized or user ID not available to save music state.");
        return;
    }
    try {
        const userMusicDocRef = firestoreDb.collection(`artifacts/${APP_ID}/users/${userId}/musicBotData`).doc('musicState');
        await userMusicDocRef.set({
            currentSong: currentSong,
            isPlaying: isPlaying,
            queue: queue,
            volume: volume,
            shuffleMode: shuffleMode,
            repeatMode: repeatMode,
            timestamp: firebase.firestore.FieldValue.serverTimestamp() // Use server timestamp
        }, { merge: true });
        console.log("Music state saved to Firestore.");
    } catch (error) {
        console.error("Error saving music state to Firestore:", error);
    }
}

// --- Event Handlers ---

// Navigation Tab Handler
navButtons.forEach(button => {
    button.addEventListener('click', () => {
        currentActiveTab = button.dataset.tab;
        updateTabContent();
    });
});

// Music Search Form Submission
searchMusicForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    searchQuery = musicSearchInput.value.trim();
    if (!searchQuery) return;

    // Simulate search results
    const simulatedResults = [
        { title: `${searchQuery} Song 1`, artist: 'Artist A', albumArt: `https://placehold.co/60x60/4A148C/FFFFFF?text=${searchQuery.substring(0,2).toUpperCase()}` },
        { title: `${searchQuery} Tune 2`, artist: 'Artist B', albumArt: `https://placehold.co/60x60/880E4F/FFFFFF?text=${searchQuery.substring(0,2).toUpperCase()}` },
        { title: `${searchQuery} Track 3`, artist: 'Artist C', albumArt: `https://placehold.co/60x60/1A237E/FFFFFF?text=${searchQuery.substring(0,2).toUpperCase()}` },
    ];
    searchResults = simulatedResults;
    updateSearchResultsUI();
    console.log(`Simulating search for: "${searchQuery}"`);
    musicSearchInput.value = ''; // Clear search input
});

// Add to Queue from Search Results (Event Delegation)
searchResultsList.addEventListener('click', async (e) => {
    if (e.target.closest('.add-to-queue-btn')) {
        const index = parseInt(e.target.closest('.add-to-queue-btn').dataset.index);
        const songToAdd = searchResults[index];
        if (songToAdd) {
            queue.push(songToAdd);
            // If nothing is playing, play the added song immediately
            if (!currentSong && !isPlaying) {
                currentSong = songToAdd;
                isPlaying = true;
                // Remove the song from the queue as it's now current
                queue.pop(); // Remove the last added song
            }
            await updateMusicStateInFirestore();
            updateQueueUI();
            updateMusicPlayerUI(); // Update now playing if a song started
            searchResults = []; // Clear search results after adding
            updateSearchResultsUI(); // Clear search results UI
            showFlashMessage(`Added "${songToAdd.title}" to queue.`, 'success');
        }
    }
});

// Play/Pause Button
playPauseBtn.addEventListener('click', async () => {
    isPlaying = !isPlaying;
    await updateMusicStateInFirestore();
    updateMusicPlayerUI(); // UI will update via Firestore listener, but trigger manually for responsiveness
});

// Previous Button (Simplified logic)
prevBtn.addEventListener('click', async () => {
    // In a real app, you'd need a playback history
    showFlashMessage('Previous song feature is simplified in this demo.', 'info');
    // For now, let's just make sure something is playing if possible
    if (!currentSong && queue.length > 0) {
        currentSong = queue.shift();
        isPlaying = true;
    } else if (currentSong) {
        // Maybe try to restart current song, or cycle back to end of queue
        showFlashMessage('No previous song in history. Restarting current or playing last in queue.', 'info');
        queue.unshift(currentSong); // Put current back to front
        currentSong = null; // Clear current to trigger next song logic
        isPlaying = true;
    } else {
        showFlashMessage('No previous song available.', 'info');
        isPlaying = false;
    }
    await updateMusicStateInFirestore();
    updateMusicPlayerUI();
    updateQueueUI();
});

// Next Button
nextBtn.addEventListener('click', async () => {
    if (queue.length > 0) {
        currentSong = queue.shift(); // Play next from queue
        isPlaying = true;
    } else {
        currentSong = null; // No more songs
        isPlaying = false;
        showFlashMessage('Queue finished!', 'info');
    }
    await updateMusicStateInFirestore();
    updateMusicPlayerUI();
    updateQueueUI();
});

// Shuffle Button
shuffleBtn.addEventListener('click', async () => {
    shuffleMode = !shuffleMode;
    // In a real app, you'd reorder the queue here if shuffle is turned on
    showFlashMessage(`Shuffle ${shuffleMode ? 'ON' : 'OFF'}`, 'info');
    await updateMusicStateInFirestore();
    updateAuthUI(); // To update button state
});

// Repeat Button
repeatBtn.addEventListener('click', async () => {
    repeatMode = !repeatMode;
    showFlashMessage(`Repeat ${repeatMode ? 'ON' : 'OFF'}`, 'info');
    await updateMusicStateInFirestore();
    updateAuthUI(); // To update button state
});

// Volume Slider
volumeSlider.addEventListener('input', async () => {
    volume = parseInt(volumeSlider.value);
    await updateMusicStateInFirestore();
    // No need to update UI here, Firestore listener handles it
});

// Add to Queue Manually (for YouTube/SoundCloud links)
addQueueItemBtn.addEventListener('click', () => {
    const url = prompt("Enter YouTube/SoundCloud URL to add to queue:");
    if (url && url.trim() !== '') {
        // Simulate adding to queue
        const newSong = { title: `Custom URL: ${url.substring(0, 30)}...`, artist: 'External Source', albumArt: 'https://placehold.co/60x60/333/FFF?text=URL' };
        queue.push(newSong);
        // If nothing is playing, play the added song immediately
        if (!currentSong && !isPlaying) {
            currentSong = newSong;
            isPlaying = true;
            queue.pop(); // Remove from queue as it's now current
        }
        updateMusicStateInFirestore(); // Update Firestore
        updateQueueUI(); // Refresh UI
        updateMusicPlayerUI();
        showFlashMessage(`Added "${newSong.title}" to queue.`, 'success');
    } else {
        showFlashMessage('No URL provided.', 'info');
    }
});

// Remove from Queue (Event Delegation on queueList)
queueList.addEventListener('click', async (e) => {
    if (e.target.closest('.remove-btn')) {
        const indexToRemove = parseInt(e.target.closest('.remove-btn').dataset.index);
        const removedSong = queue.splice(indexToRemove, 1); // Remove from array
        if (removedSong.length > 0) {
            await updateMusicStateInFirestore(); // Update Firestore
            updateQueueUI(); // Refresh UI
            showFlashMessage(`Removed "${removedSong[0].title}" from queue.`, 'info');
        }
    }
});


// AI Chat Form Submission
chatForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const message = chatInput.value.trim();
    if (message === '') return;

    chatHistory.push({ role: 'user', text: message });
    chatInput.value = ''; // Clear input
    isThinking = true; // Show thinking indicator
    updateChatUI();

    try {
        let geminiChatHistory = chatHistory.map(msg => ({
            role: msg.role === 'user' ? 'user' : 'model',
            parts: [{ text: msg.text }]
        }));

        const payload = { contents: geminiChatHistory };
        const apiKey = ""; // Canvas will provide this at runtime
        const apiUrl = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=${apiKey}`;

        const response = await fetch(apiUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(`API error: ${response.status} ${response.statusText} - ${errorData.error.message}`);
        }

        const result = await response.json();
        console.log("Gemini API response:", result);

        let aiResponseText = "Sorry, I couldn't generate a response.";
        if (result.candidates && result.candidates.length > 0 &&
            result.candidates[0].content && result.candidates[0].content.parts &&
            result.candidates[0].content.parts.length > 0) {
            aiResponseText = result.candidates[0].content.parts[0].text;
        } else {
            console.warn("Unexpected API response structure:", result);
        }

        chatHistory.push({ role: 'bot', text: aiResponseText });

    } catch (error) {
        console.error("Error calling Gemini API:", error);
        chatHistory.push({ role: 'bot', text: 'Error: Could not get a response from the AI. Please try again.' });
    } finally {
        isThinking = false;
        updateChatUI();
    }
});

// Reminder Form Submission
reminderForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = reminderTextInput.value.trim();
    const time = reminderTimeInput.value.trim();

    if (text === '' || time === '') {
        showFlashMessage('Please enter both reminder text and time.', 'info');
        return;
    }

    if (!firestoreDb || !userId) {
        console.error("Firestore not initialized or user ID not available to save reminder.");
        showFlashMessage('Application not ready to save reminders. Please refresh.', 'error');
        return;
    }

    try {
        const newReminder = {
            text: text,
            time: time, // Store as HH:MM format
            createdAt: firebase.firestore.FieldValue.serverTimestamp(),
        };
        const remindersCollectionRef = firestoreDb.collection(`artifacts/${APP_ID}/users/${userId}/reminders`);
        await remindersCollectionRef.add(newReminder);
        reminderTextInput.value = '';
        reminderTimeInput.value = '09:00'; // Reset to default
        showFlashMessage('Reminder added successfully!', 'success');
        // UI will update via Firestore listener
    } catch (error) {
        console.error("Error adding reminder:", error);
        showFlashMessage('Error adding reminder. Please try again.', 'error');
    }
});

// Delete Reminder (Event Delegation on remindersList)
remindersList.addEventListener('click', async (e) => {
    if (e.target.classList.contains('delete-btn')) {
        const reminderId = e.target.dataset.id;
        if (!reminderId) return;

        if (!firestoreDb || !userId) {
            console.error("Firestore not initialized or user ID not available to delete reminder.");
            showFlashMessage('Application not ready to delete reminders. Please refresh.', 'error');
            return;
        }

        try {
            const reminderDocRef = firestoreDb.collection(`artifacts/${APP_ID}/users/${userId}/reminders`).doc(reminderId);
            await reminderDocRef.delete();
            showFlashMessage('Reminder deleted successfully!', 'success');
            // UI will update via Firestore listener
        } catch (error) {
            console.error("Error deleting reminder:", error);
            showFlashMessage('Error deleting reminder. Please try again.', 'error');
        }
    }
});

// Simulate a safety alert after 15 seconds (once on load)
setTimeout(() => {
    safetyAlerts.push({
        id: Date.now(),
        type: 'Command Abuse',
        message: 'Multiple rapid /play commands detected from user.',
        timestamp: new Date().toLocaleTimeString()
    });
    updateSafetyAlertsUI();
    showFlashMessage('New Security Alert: Command Abuse Detected!', 'error');
}, 15000);

// Function to display flash messages
function showFlashMessage(message, category) {
    const flashDiv = document.createElement('div');
    flashDiv.className = `flash-message ${category}`;
    flashDiv.textContent = message;
    flashMessagesContainer.appendChild(flashDiv);

    setTimeout(() => {
        flashDiv.style.animation = 'fadeOut 0.5s ease-out forwards';
        flashDiv.addEventListener('animationend', () => flashDiv.remove());
    }, 5000); // Message stays for 5 seconds
}

// Initial UI updates after DOM is fully loaded and before Firebase might update it
// (These are handled by the DOMContentLoaded listener now, and subsequent updates from Firebase listeners)

// Set up periodic polling for now playing data from Flask (for actual bot status)
setInterval(async () => {
    try {
        const response = await fetch(BASE_URL + 'api/now_playing_data');
        const data = await response.json();

        // Update local state based on Flask API
        // This is a simplified merge, real app might need more granular sync
        if (data.status === 'playing_spotify' || data.status === 'playing_youtube') {
            currentSong = {
                title: data.title,
                artist: data.artist,
                albumArt: data.album_cover_url
            };
            isPlaying = true;
            // For YouTube playback, Flask provides duration/progress. Spotify provides it too.
            // Implement progress bar update using these values
            const totalMs = data.duration_ms || 0;
            const currentMs = data.progress_ms || 0;
            const progressPercent = totalMs > 0 ? (currentMs / totalMs) * 100 : 0;
            progressBar.style.width = `${progressPercent}%`;
            currentTimeDisplay.textContent = formatTime(currentMs);
            totalTimeDisplay.textContent = formatTime(totalMs);

        } else if (data.status === 'spotify_paused_or_stopped' || data.status === 'youtube_paused') {
            // Update to paused state
            currentSong = {
                title: data.title || 'Paused',
                artist: data.artist || 'No music playing',
                albumArt: data.album_cover_url || 'https://placehold.co/400x400/3498db/ffffff?text=No+Music'
            };
            isPlaying = false;
            const totalMs = data.duration_ms || 0;
            const currentMs = data.progress_ms || 0;
            const progressPercent = totalMs > 0 ? (currentMs / totalMs) * 100 : 0;
            progressBar.style.width = `${progressPercent}%`;
            currentTimeDisplay.textContent = formatTime(currentMs);
            totalTimeDisplay.textContent = formatTime(totalMs);

        }
        else {
            currentSong = null;
            isPlaying = false;
            // Reset progress bar
            progressBar.style.width = '0%';
            currentTimeDisplay.textContent = '0:00';
            totalTimeDisplay.textContent = '0:00';
        }
        updateMusicPlayerUI(); // Update UI based on fetched data
    } catch (error) {
        console.error('Error fetching now playing data from Flask:', error);
        // Fallback UI for error
        currentSong = null;
        isPlaying = false;
        updateMusicPlayerUI();
    }
}, 3000); // Poll every 3 seconds

// Set up periodic polling for queue data from Flask
setInterval(async () => {
    try {
        const response = await fetch(BASE_URL + 'api/queue_data');
        const data = await response.json();
        // Flask queue items are just URLs, so we'll simulate detailed info for display
        // In a real scenario, you'd want the Flask backend to return more details for each queue item.
        queue = (data.queue || []).map(item => ({
            title: item.length > 50 ? `(URL) ${item.substring(0, 47)}...` : `(URL) ${item}`,
            artist: 'External Source',
            albumArt: 'https://placehold.co/40x40/333/FFF?text=URL'
        }));
        updateQueueUI();
    } catch (error) {
        console.error('Error fetching queue data from Flask:', error);
        queue = []; // Clear queue on error
        updateQueueUI();
    }
}, 5000); // Poll every 5 seconds

// Helper to format time (from milliseconds)
function formatTime(ms) {
    const totalSeconds = Math.floor(ms / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}:${seconds < 10 ? '0' : ''}${seconds}`;
}
