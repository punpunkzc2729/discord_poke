// Ensure Firebase is loaded before this script runs.
// The firebase-app-compat.js, firebase-auth-compat.js, firebase-firestore-compat.js
// scripts are loaded via CDN in index.html, making `firebase` global.

// --- Firebase Initialization ---
let firebaseApp;
let firestoreDb;
let firebaseAuth;
let userId = null; // Store authenticated user ID

document.addEventListener('DOMContentLoaded', async () => {
    try {
        // Check if FIREBASE_CONFIG is defined globally from Flask
        if (typeof FIREBASE_CONFIG !== 'undefined' && Object.keys(FIREBASE_CONFIG).length > 0) {
            firebaseApp = firebase.initializeApp(FIREBASE_CONFIG);
            firestoreDb = firebase.firestore();
            firebaseAuth = firebase.auth();

            // Firebase Authentication listener
            firebaseAuth.onAuthStateChanged(async (user) => {
                if (user) {
                    // User is signed in.
                    userId = user.uid;
                    console.log("Firebase user signed in:", userId);
                    // Update UI or fetch user-specific data here
                } else {
                    // User is signed out.
                    console.log("Firebase user signed out.");
                    userId = null;
                    // Attempt anonymous sign-in if no token provided, or if token expired
                    if (typeof INITIAL_AUTH_TOKEN !== 'undefined' && INITIAL_AUTH_TOKEN) {
                        try {
                            await firebaseAuth.signInWithCustomToken(INITIAL_AUTH_TOKEN);
                            console.log("Signed in with custom token.");
                        } catch (error) {
                            console.error("Error signing in with custom token:", error);
                            // Fallback to anonymous if custom token fails
                            await firebaseAuth.signInAnonymously();
                            console.log("Signed in anonymously after custom token failure.");
                        }
                    } else {
                        // Sign in anonymously if no initial token
                        await firebaseAuth.signInAnonymously();
                        console.log("Signed in anonymously.");
                    }
                }
                // Initial fetch after auth state is determined
                updateAuthUI();
                fetchNowPlayingAndQueue();
            });
        } else {
            console.error("Firebase config is missing or empty. Firebase will not be initialized.");
            showFlashMessage("Error: Firebase configuration missing.", "error");
            // Still proceed to update UI based on Flask's auth status
            updateAuthUI();
            fetchNowPlayingAndQueue();
        }
    } catch (error) {
        console.error("Firebase initialization or authentication error:", error);
        showFlashMessage("Error initializing Firebase: " + error.message, "error");
        // Ensure UI is updated even if Firebase init fails
        updateAuthUI();
        fetchNowPlayingAndQueue();
    }
});


// --- DOM Elements ---
const welcomeState = document.getElementById('welcomeState');
const nowPlayingState = document.getElementById('nowPlayingState');
const currentCoverImage = document.getElementById('currentCoverImage');
const currentSongTitle = document.getElementById('currentSongTitle');
const currentArtistName = document.getElementById('currentArtistName');
const elapsedTimeSpan = document.getElementById('elapsedTime');
const totalTimeSpan = document.getElementById('totalTime');
const actualProgressBar = document.getElementById('actualProgressBar');

const shuffleBtn = document.getElementById('shuffleBtn');
const prevBtn = document.getElementById('prevBtn');
const playPauseBtn = document.getElementById('playPauseBtn');
const playIcon = document.getElementById('playIcon');
const pauseIcon = document.getElementById('pauseIcon');
const nextBtn = document.getElementById('nextBtn');
const loopBtn = document.getElementById('loopBtn');
const volumeSlider = document.getElementById('volumeSlider');
const searchOrUrlInput = document.getElementById('searchOrUrlInput');
const searchForm = document.querySelector('.now-playing-card form'); // Get the form for submit listener

const queueContentDiv = document.getElementById('queueContent');
const queueListUl = document.getElementById('queueList');

const discordStatusDiv = document.getElementById('discordStatus');
const spotifyStatusDiv = document.getElementById('spotifyStatus');
const spotifyLoginLink = document.getElementById('spotifyLoginLink');


// --- State Variables ---
let currentPlaybackData = null; // Stores data from /api/now_playing_data
let updateInterval;


// --- Utility Functions ---
function formatTime(seconds) {
    if (isNaN(seconds) || seconds < 0) return "0:00";
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = Math.floor(seconds % 60);
    return `${minutes}:${remainingSeconds < 10 ? '0' : ''}${remainingSeconds}`;
}

function showFlashMessage(message, type = 'info') {
    const flashContainer = document.querySelector('.main-container .mb-6.space-y-3'); 
    if (!flashContainer) return;

    const flashDiv = document.createElement('div');
    flashDiv.className = `p-4 rounded-lg text-sm bg-${type}-600 bg-opacity-80 shadow-md`; 
    flashDiv.textContent = message;
    flashContainer.appendChild(flashDiv);
    
    setTimeout(() => {
        flashDiv.style.transition = 'opacity 0.5s ease-out';
        flashDiv.style.opacity = '0';
        setTimeout(() => flashDiv.remove(), 500);
    }, 5000);
}

// --- API Interaction Functions ---

async function fetchAuthStatus() {
    try {
        const response = await fetch(urls.getAuthStatus);
        if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
        const data = await response.json();
        return data;
    } catch (error) {
        console.error('Error fetching auth status:', error);
        showFlashMessage('Failed to fetch authentication status.', 'error');
        return { is_discord_linked: false, is_spotify_linked: false, discord_user_id: null, discord_username: null };
    }
}

async function fetchNowPlayingAndQueue() {
    try {
        const nowPlayingResponse = await fetch(urls.getNowPlayingData);
        if (!nowPlayingResponse.ok) throw new Error(`HTTP error! status: ${nowPlayingResponse.status}`);
        currentPlaybackData = await nowPlayingResponse.json();
        updateNowPlayingUI(currentPlaybackData);

        const queueResponse = await fetch(urls.getQueueData);
        if (!queueResponse.ok) throw new Error(`HTTP error! status: ${queueResponse.status}`);
        const queueData = await queueResponse.json();
        updateQueueUI(queueData.queue);

    } catch (error) {
        console.error('Error fetching now playing or queue data from Flask:', error);
        showFlashMessage('Failed to fetch music data.', 'error');
        // Set to default state if fetching fails
        updateNowPlayingUI({ status: "error", is_playing: false, is_paused: false });
        updateQueueUI([]);
    }
}

async function sendControlCommand(url, method = 'POST', body = null) {
    try {
        const options = { method: method };
        if (body) {
            options.body = body instanceof FormData ? body : JSON.stringify(body);
            if (!(body instanceof FormData)) {
                options.headers = { 'Content-Type': 'application/json' };
            }
        }
        const response = await fetch(url, options);
        const result = await response.json(); // Always expect JSON response from Flask APIs
        if (result.status === "error" || result.status === "warning") {
            showFlashMessage(result.message, result.status);
        } else {
            // No need to show success messages here, as UI updates via polling
            // showFlashMessage(result.message, result.status);
        }
        // Immediately trigger an update after a control command
        fetchNowPlayingAndQueue(); 
    } catch (error) {
        console.error('Error sending control command:', error);
        showFlashMessage('Error performing action. Check console for details.', 'error');
    }
}

// --- UI Update Functions ---

async function updateAuthUI() {
    const authData = await fetchAuthStatus();
    const discordUserId = authData.discord_user_id;

    // Discord Status
    discordStatusDiv.innerHTML = ''; // Clear current content
    if (authData.is_discord_linked) {
        const span = document.createElement('span');
        span.className = "text-green-400 font-medium";
        span.textContent = `เชื่อมโยงแล้ว (ID: ${discordUserId || 'Fetching...'})`;
        discordStatusDiv.appendChild(span);
    } else {
        const span = document.createElement('span');
        span.className = "text-red-400 font-medium";
        span.textContent = "ยังไม่เชื่อมโยง";
        const link = document.createElement('a');
        link.href = urls.loginDiscord;
        link.className = "px-4 py-2 bg-blue-600 hover:bg-blue-700 rounded-lg shadow-md transition duration-300 text-sm";
        link.textContent = "เข้าสู่ระบบ Discord";
        discordStatusDiv.appendChild(span);
        discordStatusDiv.appendChild(link);
    }

    // Spotify Status
    spotifyStatusDiv.innerHTML = ''; // Clear current content
    if (authData.is_spotify_linked) {
        const span = document.createElement('span');
        span.className = "text-green-400 font-medium";
        span.textContent = "เชื่อมโยงแล้ว";
        spotifyStatusDiv.appendChild(span);
    } else {
        const span = document.createElement('span');
        span.className = "text-red-400 font-medium";
        span.textContent = "ยังไม่เชื่อมโยง";
        if (authData.is_discord_linked && discordUserId) {
            const link = document.createElement('a');
            // Replace placeholder with actual Discord User ID
            link.href = urls.loginSpotifyWeb.replace('__DISCORD_USER_ID__', discordUserId);
            link.className = "px-4 py-2 bg-green-600 hover:bg-green-700 rounded-lg shadow-md transition duration-300 text-sm";
            link.textContent = "เชื่อมโยง Spotify";
            spotifyStatusDiv.appendChild(link);
        } else {
            const spanDisabled = document.createElement('span');
            spanDisabled.className = "text-gray-400 text-sm";
            spanDisabled.textContent = "เข้าสู่ระบบ Discord ก่อน";
            spotifyStatusDiv.appendChild(spanDisabled);
        }
    }
}


function updateNowPlayingUI(data) {
    if (data.is_playing || data.is_paused) {
        welcomeState.classList.add('hidden');
        nowPlayingState.classList.remove('hidden');

        currentCoverImage.src = data.album_cover_url || "https://placehold.co/400x400/94a3b8/ffffff?text=No+Cover";
        currentSongTitle.textContent = data.title || "Unknown Song";
        currentArtistName.textContent = data.artist || "Unknown Artist";

        // Update play/pause button icon
        if (data.is_playing) {
            playIcon.style.display = 'none';
            pauseIcon.style.display = 'block';
        } else {
            playIcon.style.display = 'block';
            pauseIcon.style.display = 'none';
        }

        // Update progress bar and time
        const progressPercentage = (data.progress_ms / data.duration_ms) * 100;
        actualProgressBar.style.width = `${progressPercentage}%`;
        elapsedTimeSpan.textContent = formatTime(data.progress_ms / 1000);
        totalTimeSpan.textContent = formatTime(data.duration_ms / 1000);

        // Update volume slider (Flask provides volume 0.0-2.0, convert to 0-100)
        volumeSlider.value = (data.volume * 100 / 2).toFixed(0);

        // Update shuffle and loop button states
        if (data.is_shuffling) {
            shuffleBtn.classList.add('text-purple-400'); // Highlight active
        } else {
            shuffleBtn.classList.remove('text-purple-400');
        }
        if (data.is_looping) {
            loopBtn.classList.add('text-purple-400'); // Highlight active
        } else {
            loopBtn.classList.remove('text-purple-400');
        }

    } else {
        welcomeState.classList.remove('hidden');
        nowPlayingState.classList.add('hidden');
    }
}

function updateQueueUI(queue) {
    queueListUl.innerHTML = ''; // Clear existing list
    if (queue && queue.length > 0) {
        queueContentDiv.classList.remove('hidden');
        queueListUl.classList.remove('hidden');
        queue.forEach((item, index) => {
            const listItem = document.createElement('li');
            listItem.className = "flex items-center space-x-3 p-2 bg-gray-600 bg-opacity-50 rounded-lg";
            listItem.innerHTML = `
                <span class="font-bold text-purple-300">${index + 1}.</span>
                <span class="text-white truncate">${item}</span>
            `;
            queueListUl.appendChild(listItem);
        });
        // Hide the "Queue is empty" message if there are items
        const emptyMessage = queueContentDiv.querySelector('p');
        if (emptyMessage) emptyMessage.classList.add('hidden');

    } else {
        queueContentDiv.classList.remove('hidden');
        queueListUl.classList.add('hidden');
        // Show the "Queue is empty" message
        const emptyMessage = queueContentDiv.querySelector('p');
        if (emptyMessage) emptyMessage.classList.remove('hidden');
        else { // If it was removed, re-add it
            const p = document.createElement('p');
            p.className = "text-purple-200 text-lg text-center mt-8";
            p.textContent = "Queue is empty. Search for songs to add!";
            queueContentDiv.appendChild(p);
        }
    }
}

// --- Event Listeners ---

// Submit form for search/add to queue
searchForm.addEventListener('submit', async (e) => {
    e.preventDefault(); // Prevent default form submission
    const query = searchOrUrlInput.value.trim();
    if (query) {
        await sendControlCommand(urls.addOrSearch, 'POST', new FormData(searchForm)); // Pass form data
        searchOrUrlInput.value = ''; // Clear input
    } else {
        showFlashMessage('Please enter a song name, artist, or URL.', 'error');
    }
});


playPauseBtn.addEventListener('click', async () => {
    if (currentPlaybackData && currentPlaybackData.is_playing) {
        await sendControlCommand(urls.pauseControl);
    } else {
        await sendControlCommand(urls.resumeControl);
    }
});

prevBtn.addEventListener('click', async () => {
    // This will only work for Spotify if the user is connected to Spotify and playing Spotify.
    // Bot's internal queue doesn't have "previous" functionality easily.
    await sendControlCommand(urls.skipPreviousControl);
});

nextBtn.addEventListener('click', async () => {
    await sendControlCommand(urls.skipControl);
});

shuffleBtn.addEventListener('click', async () => {
    await sendControlCommand(urls.toggleShuffle, 'POST');
});

loopBtn.addEventListener('click', async () => {
    await sendControlCommand(urls.toggleLoop, 'POST');
});

volumeSlider.addEventListener('input', async (e) => {
    const vol = e.target.value; // Value from 0 to 100
    // Convert to Flask's expected range (0.0 to 2.0)
    const normalizedVol = (vol / 100) * 2.0; 
    await sendControlCommand(`${urls.setVolumeControl}?vol=${normalizedVol}`, 'GET');
});


// Initial load and periodic updates
document.addEventListener('DOMContentLoaded', () => {
    updateAuthUI(); // Initial auth UI update
    fetchNowPlayingAndQueue(); // Initial music data fetch

    // Set up periodic update for now playing and queue data
    updateInterval = setInterval(fetchNowPlayingAndQueue, 5000); // Update every 5 seconds

    // Add event listener for general flash messages fade out (Flask rendered)
    const flashMessages = document.querySelectorAll('.flash-message');
    flashMessages.forEach(msg => {
        setTimeout(() => {
            msg.style.opacity = '0';
            setTimeout(() => msg.remove(), 500);
        }, 5000);
    });
});
