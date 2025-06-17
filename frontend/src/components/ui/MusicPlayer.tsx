    import React, { useState, useEffect } from 'react';
    import { Search, Play, Pause, FastForward, Rewind, Stop, VolumeUp, Volume2, VolumeX, Plus } from 'lucide-react';
    import { Button } from './ui/button';
    import { Card, CardContent, CardDescription, CardHeader, CardTitle } from './ui/card';
    import { useAuth, useBaseUrl } from '../App'; // Import contexts

    interface MusicPlayerProps {
      isSpotifyLinked: boolean;
      discordUserId: string | null;
      onConnectSpotify: () => void;
    }

    const MusicPlayer: React.FC<MusicPlayerProps> = ({ isSpotifyLinked, discordUserId, onConnectSpotify }) => {
      const { BASE_URL } = useBaseUrl();

      const [musicSearchInput, setMusicSearchInput] = useState('');
      const [songTitle, setSongTitle] = useState('Welcome to your Music Bot');
      const [artistName, setArtistName] = useState('Get started by searching for a song');
      const [coverImage, setCoverImage] = useState('https://placehold.co/400x400/3498db/ffffff?text=No+Music');
      const [isPlaying, setIsPlaying] = useState(false);
      const [currentTrackDuration, setCurrentTrackDuration] = useState(0); // in ms
      let [currentTrackProgress, setCurrentTrackProgress] = useState(0); // in ms
      const [queue, setQueue] = useState<string[]>([]);
      const [volume, setVolume] = useState(100); // 0-200 mapped to 0.0-2.0

      let playbackProgressInterval: NodeJS.Timeout | null = null;

      // Helper to format time from milliseconds to MM:SS
      const formatTime = (ms: number) => {
        const totalSeconds = Math.floor(ms / 1000);
        const minutes = Math.floor(totalSeconds / 60);
        const seconds = totalSeconds % 60;
        return `${minutes}:${seconds < 10 ? '0' : ''}${seconds}`;
      };

      // Function to fetch authentication status from Flask backend API
      const fetchNowPlayingData = async () => {
          try {
              const response = await fetch(BASE_URL + 'api/now_playing_data');
              if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
              const data = await response.json();

              if (data.status === 'playing_spotify' || data.status === 'playing_youtube') {
                  setSongTitle(data.title);
                  setArtistName(data.artist);
                  setCoverImage(data.album_cover_url);
                  setCurrentTrackDuration(data.duration_ms);
                  setCurrentTrackProgress(data.progress_ms); // Spotify provides actual progress
                  setIsPlaying(true);
                  if (!playbackProgressInterval) { // Only start if not already running
                      startProgressInterval(); 
                  }
              } else if (data.status === 'spotify_paused_or_stopped' || data.status === 'youtube_paused') {
                  setSongTitle(data.title || 'Paused/Stopped');
                  setArtistName(data.artist || 'No music playing');
                  setCoverImage(data.album_cover_url || 'https://placehold.co/400x400/3498db/ffffff?text=No+Music');
                  setCurrentTrackDuration(data.duration_ms || 0);
                  setCurrentTrackProgress(data.progress_ms || 0);
                  setIsPlaying(false);
                  if (playbackProgressInterval) {
                      clearInterval(playbackProgressInterval);
                      playbackProgressInterval = null;
                  }
              } else { // no_music_playing, spotify_error, not_logged_in, error
                  setSongTitle('Welcome to your Music Bot');
                  setArtistName('Get started by searching for a song');
                  setCoverImage('https://placehold.co/400x400/3498db/ffffff?text=No+Music');
                  setIsPlaying(false);
                  setCurrentTrackDuration(0);
                  setCurrentTrackProgress(0);
                  if (playbackProgressInterval) {
                      clearInterval(playbackProgressInterval);
                      playbackProgressInterval = null;
                  }
              }
          } catch (error) {
              console.error('Error fetching now playing data:', error);
              // Handle error, e.g., show a message, revert UI state
              setSongTitle('Error loading music');
              setArtistName('Please try again');
              setCoverImage('https://placehold.co/400x400/FF0000/FFFFFF?text=Error');
              setIsPlaying(false);
              setCurrentTrackDuration(0);
              setCurrentTrackProgress(0);
              if (playbackProgressInterval) {
                  clearInterval(playbackProgressInterval);
                  playbackProgressInterval = null;
              }
          }
      };

      const startProgressInterval = () => {
        if (playbackProgressInterval) {
          clearInterval(playbackProgressInterval);
        }
        playbackProgressInterval = setInterval(() => {
          setCurrentTrackProgress(prev => {
            const newProgress = prev + 1000;
            if (newProgress >= currentTrackDuration && currentTrackDuration > 0) {
              clearInterval(playbackProgressInterval!);
              playbackProgressInterval = null;
              // Trigger a refresh after song ends to fetch next song
              fetchNowPlayingData();
              fetchQueueData();
              return currentTrackDuration;
            }
            return newProgress;
          });
        }, 1000);
      };

      const fetchQueueData = async () => {
        try {
          const response = await fetch(BASE_URL + 'api/queue_data');
          if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
          const data = await response.json();
          setQueue(data.queue || []);
        } catch (error) {
          console.error('Error fetching queue data:', error);
          setQueue(['Error loading queue']);
        }
      };

      useEffect(() => {
        // Initial fetch and set up polling
        fetchNowPlayingData();
        fetchQueueData();

        const nowPlayingInterval = setInterval(fetchNowPlayingData, 5000); // Poll every 5 seconds
        const queueInterval = setInterval(fetchQueueData, 10000); // Poll every 10 seconds

        return () => {
          clearInterval(nowPlayingInterval);
          clearInterval(queueInterval);
          if (playbackProgressInterval) {
            clearInterval(playbackProgressInterval);
          }
        };
      }, [BASE_URL]);

      // --- Event Handlers ---
      const handleSearchMusic = async () => {
        const query = musicSearchInput.trim();
        if (!query) {
          alert('กรุณาป้อนชื่อเพลง ศิลปิน หรืออัลบั้มเพื่อค้นหา.');
          return;
        }
        try {
          const response = await fetch(BASE_URL + 'web_control/play_spotify_search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query }),
          });
          const result = await response.json();
          if (response.ok) {
            alert(result.message); // Use alert for simplicity, integrate flash messages later
            fetchNowPlayingData(); // Refresh UI after action
          } else {
            alert(result.message || 'Error searching music.');
          }
        } catch (error) {
          console.error('Error sending search query:', error);
          alert('Failed to send search query. Please try again.');
        }
        setMusicSearchInput('');
      };

      const handlePlayPause = async () => {
        if (isPlaying) {
          await fetch(BASE_URL + 'web_control/pause');
        } else {
          await fetch(BASE_URL + 'web_control/resume');
        }
        fetchNowPlayingData(); // Refresh UI immediately
      };

      const handleSkipPrevious = async () => {
        await fetch(BASE_URL + 'web_control/skip_previous');
        fetchNowPlayingData(); // Refresh UI immediately
      };

      const handleSkipNext = async () => {
        await fetch(BASE_URL + 'web_control/skip');
        fetchNowPlayingData(); // Refresh UI immediately
        fetchQueueData(); // Queue might change
      };

      const handleStop = async () => {
        await fetch(BASE_URL + 'web_control/stop');
        fetchNowPlayingData(); // Refresh UI immediately
        fetchQueueData(); // Queue is cleared
      };

      const handleVolumeChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const newVolume = parseInt(e.target.value);
        setVolume(newVolume); // Update local state immediately for smooth slider
        const mappedVolume = newVolume / 100; // Map 0-200 to 0.0-2.0
        try {
          await fetch(`${BASE_URL}web_control/set_volume?vol=${mappedVolume}`);
          // Flash message for volume change can be added here
        } catch (error) {
          console.error("Error setting volume:", error);
        }
      };

      const handleAddQueueItem = () => {
        const url = prompt("กรุณาป้อน YouTube/SoundCloud URL ที่จะเพิ่มเข้าคิว:");
        if (url) {
          fetch(BASE_URL + 'web_control/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: url.trim() }),
          })
          .then(response => response.json())
          .then(data => {
            if (data.status === 'success') {
              alert(data.message);
              fetchQueueData(); // Refresh queue display
              fetchNowPlayingData(); // Check if playback started
            } else {
              alert(data.message || 'ไม่สามารถเพิ่มลงในคิวได้ กรุณาลองใหม่.');
            }
          })
          .catch(error => {
            console.error('Error adding to queue:', error);
            alert('ไม่สามารถเพิ่มลงในคิวได้ กรุณาลองใหม่.');
          });
        }
      };

      // Display logic for auth messages
      const authStatusMessage = isDiscordLinked
        ? (isSpotifyLinked ? 'เชื่อมต่อ Discord และ Spotify แล้ว' : 'เชื่อมต่อ Discord แล้ว (กรุณาเชื่อมต่อ Spotify)')
        : 'กรุณา Login ด้วย Discord';

      return (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 p-6">
            {/* Music Player Section (Left Side) */}
            <div className="section-card lg:col-span-2">
                <CardHeader>
                    <CardTitle className="text-2xl font-bold text-white mb-4">Music Player</CardTitle>
                </CardHeader>
                <CardContent>
                    <h2 className="text-xl font-semibold mb-4 text-purple-200">Search Music</h2>
                    <div className="search-bar flex gap-2 mb-6">
                        <input 
                            type="text" 
                            id="musicSearchInput" 
                            placeholder="Search for songs, artists, or Spotify links..." 
                            className="flex-grow p-3 rounded-lg border border-gray-700 bg-gray-800 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-purple-600"
                            value={musicSearchInput}
                            onChange={(e) => setMusicSearchInput(e.target.value)}
                            disabled={!isDiscordLinked || !isSpotifyLinked}
                        />
                        <Button 
                            className="bg-purple-600 hover:bg-purple-700 text-white rounded-lg px-4 py-2 flex items-center justify-center"
                            onClick={handleSearchMusic}
                            disabled={!isDiscordLinked || !isSpotifyLinked}
                        >
                            <Search className="h-5 w-5" />
                        </Button>
                    </div>

                    <div className="login-buttons-container flex flex-col items-center gap-4 mb-8">
                        {!isDiscordLinked && (
                            <Button className="bg-blue-600 hover:bg-blue-700 text-white rounded-lg px-6 py-3 text-lg font-semibold flex items-center gap-2" onClick={() => window.location.href = BASE_URL + 'login/discord'}>
                                <i className="fab fa-discord"></i> Login with Discord
                            </Button>
                        )}
                        {isDiscordLinked && !isSpotifyLinked && (
                            <Button className="bg-spotify-green hover:bg-green-700 text-white rounded-lg px-6 py-3 text-lg font-semibold flex items-center gap-2" onClick={onConnectSpotify}>
                                <i className="fab fa-spotify"></i> Connect Spotify
                            </Button>
                        )}
                        <p className="text-sm text-gray-400">{authStatusMessage}</p>
                    </div>

                    <h2 className="text-xl font-semibold mb-4 text-purple-200">Now Playing</h2>
                    <Card className="now-playing-card bg-white/5 border-purple-500/20 shadow-md p-6 flex flex-col items-center text-white rounded-xl">
                        <div className="album-cover w-48 h-48 rounded-xl overflow-hidden mb-4 shadow-xl">
                            <img src={coverImage} alt="Album Cover" className="w-full h-full object-cover" />
                        </div>
                        <div className="song-details text-center mb-4">
                            <div className="title text-2xl font-bold">{songTitle}</div>
                            <div className="artist text-gray-400">{artistName}</div>
                        </div>
                        <div className="progress-bar-container w-full bg-gray-700 h-1.5 rounded-full mb-2">
                            <div className="progress-bar-fill bg-purple-500 h-full rounded-full" style={{ width: `${(currentTrackProgress / currentTrackDuration) * 100 || 0}%` }}></div>
                        </div>
                        <div className="time-display flex justify-between w-full text-xs text-gray-400 mb-6">
                            <span>{formatTime(currentTrackProgress)}</span>
                            <span>{formatTime(currentTrackDuration)}</span>
                        </div>
                        <div className="player-controls flex items-center justify-center gap-4 mb-4">
                            <Button variant="ghost" className="bg-white/10 hover:bg-white/20 p-2 rounded-full h-12 w-12 flex items-center justify-center" onClick={handleSkipPrevious}>
                                <Rewind className="h-6 w-6 text-white" />
                            </Button>
                            <Button variant="ghost" className="bg-purple-600 hover:bg-purple-700 p-3 rounded-full h-16 w-16 flex items-center justify-center" onClick={handlePlayPause}>
                                {isPlaying ? <Pause className="h-8 w-8 text-white" /> : <Play className="h-8 w-8 text-white" />}
                            </Button>
                            <Button variant="ghost" className="bg-white/10 hover:bg-white/20 p-2 rounded-full h-12 w-12 flex items-center justify-center" onClick={handleSkipNext}>
                                <FastForward className="h-6 w-6 text-white" />
                            </Button>
                            <Button variant="ghost" className="bg-white/10 hover:bg-white/20 p-2 rounded-full h-12 w-12 flex items-center justify-center" onClick={handleStop}>
                                <Stop className="h-6 w-6 text-white" />
                            </Button>
                        </div>
                        <div className="volume-control flex items-center gap-3 w-full justify-center">
                            <VolumeX className="h-5 w-5 text-gray-400" />
                            <input 
                                type="range" 
                                min="0" 
                                max="200" 
                                value={volume} 
                                onChange={handleVolumeChange}
                                className="volume-slider w-48 appearance-none h-1 bg-gray-700 rounded-full cursor-pointer [&::-webkit-slider-thumb]:bg-purple-500 [&::-webkit-slider-thumb]:h-4 [&::-webkit-slider-thumb]:w-4 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:appearance-none"
                            />
                            <VolumeUp className="h-5 w-5 text-gray-400" />
                        </div>
                    </CardContent>
                </CardHeader>
            </div>

            {/* Queue Section (Right Side) */}
            <div className="section-card lg:col-span-1">
                <CardHeader className="flex flex-row justify-between items-center pb-2">
                    <CardTitle className="text-2xl font-bold text-white">Queue</CardTitle>
                    <Button 
                        className="bg-purple-600 hover:bg-purple-700 text-white rounded-lg px-3 py-1 text-sm flex items-center gap-1"
                        onClick={handleAddQueueItem}
                        disabled={!isDiscordLinked}
                    >
                        <Plus className="h-4 w-4" /> Add
                    </Button>
                </CardHeader>
                <CardContent className="pt-2">
                    <ul className="queue-list max-h-96 overflow-y-auto pr-2">
                        {queue.length > 0 ? (
                            queue.map((item, index) => (
                                <li key={index} className="flex items-center justify-between py-2 border-b border-gray-700 last:border-b-0">
                                    <div className="song-details flex flex-col text-sm">
                                        <div className="title font-semibold text-white">{item}</div>
                                        <div className="artist text-gray-400"></div> {/* No artist for plain URLs */}
                                    </div>
                                    {/* <div className="duration text-gray-400"></div> */} {/* No duration for plain URLs */}
                                </li>
                            ))
                        ) : (
                            <li className="text-center text-gray-400 py-4">Queue is empty. Add some music!</li>
                        )}
                    </ul>
                </CardContent>
            </div>
        </div>
      );
    };

    export { MusicPlayer };
    