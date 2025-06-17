    import React, { useState, useEffect } from 'react';
    import { Music, Users, Settings, Calendar, Shield, Bot } from 'lucide-react';
    // Import Shadcn UI components
    import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/card';
    import { Button } from '../components/ui/button';
    // Import contexts
    import { useAuth, useBaseUrl } from '../App';
    // Import placeholder components for sub-panels (since full implementation is out of scope for now)
    import { MusicPlayer } from '../components/MusicPlayer'; // This component will be added below
    // You might want to create these as separate files under components/
    const ServerStats = () => <div className="p-6 text-center text-gray-400">Server Stats panel coming soon...</div>;
    const RemindersPanel = () => <div className="p-6 text-center text-gray-400">Reminders panel coming soon...</div>;
    const SecurityAlerts = () => <div className="p-6 text-center text-gray-400">Security Alerts panel coming soon...</div>;


    const Dashboard = () => {
      const { 
        isDiscordLinked, 
        isSpotifyLinked, 
        discordUserId, 
        discordUsername,
        fetchAuthStatus, // To re-fetch auth status if needed
        setCurrentPage // To navigate back to index if disconnected
      } = useAuth();
      const { BASE_URL } = useBaseUrl();

      const [activeTab, setActiveTab] = useState('music');
      
      const navItems = [
        { id: 'music', label: 'Music Player', icon: Music },
        { id: 'stats', label: 'Server Stats', icon: Users },
        { id: 'reminders', label: 'Reminders', icon: Calendar },
        { id: 'security', label: 'Security', icon: Shield },
        { id: 'settings', label: 'Settings', icon: Settings }
      ];

      useEffect(() => {
        // Redirect to index if somehow not linked
        if (!isDiscordLinked) {
            setCurrentPage('index');
        }
      }, [isDiscordLinked, setCurrentPage]);

      const handleConnectDiscord = () => {
        window.location.href = BASE_URL + 'login/discord';
      };

      const renderContent = () => {
        switch (activeTab) {
          case 'music':
            return (
              <MusicPlayer 
                isSpotifyLinked={isSpotifyLinked}
                discordUserId={discordUserId}
                onConnectSpotify={() => window.location.href = BASE_URL + `login/spotify/${discordUserId}`}
              />
            );
          case 'stats':
            return <ServerStats />;
          case 'reminders':
            return <RemindersPanel />;
          case 'security':
            return <SecurityAlerts />;
          case 'settings':
            return <div className="p-6 text-center text-gray-400">Settings panel coming soon...</div>;
          default:
            return <MusicPlayer 
              isSpotifyLinked={isSpotifyLinked}
              discordUserId={discordUserId}
              onConnectSpotify={() => window.location.href = BASE_URL + `login/spotify/${discordUserId}`}
            />;
        }
      };

      return (
        <div className="min-h-screen bg-gradient-to-br from-purple-900 via-blue-900 to-indigo-900 p-4 md:p-8 flex flex-col items-center">
          <div className="w-full max-w-6xl">
            {/* Header */}
            <div className="flex flex-col md:flex-row items-center justify-between p-4 bg-white/10 backdrop-blur-lg rounded-xl mb-6 shadow-lg border border-purple-400/30">
              <div className="flex items-center space-x-4 mb-4 md:mb-0">
                <Bot className="h-10 w-10 text-purple-400" />
                <div>
                  <h1 className="text-3xl font-bold text-white">Discord Music Bot</h1>
                  <p className="text-purple-200">AI-Enhanced Music & Community Assistant</p>
                </div>
              </div>
              {!isDiscordLinked ? (
                <Button variant="outline" className="text-white border-purple-400 hover:bg-purple-800" onClick={handleConnectDiscord}>
                  Connect Discord
                </Button>
              ) : (
                <div className="text-white text-lg">
                  Logged in as: <span className="font-semibold text-purple-300">{discordUsername || `User ID: ${discordUserId}`}</span>
                </div>
              )}
            </div>

            {/* Navigation */}
            <div className="flex space-x-2 mb-6 overflow-x-auto bg-white/10 backdrop-blur-lg rounded-xl p-2 shadow-lg border border-purple-400/30">
              {navItems.map((item) => {
                const Icon = item.icon;
                return (
                  <Button
                    key={item.id}
                    variant={activeTab === item.id ? "default" : "ghost"}
                    onClick={() => setActiveTab(item.id)}
                    className={`flex items-center space-x-2 whitespace-nowrap px-4 py-2 rounded-lg 
                      ${activeTab === item.id 
                        ? "bg-purple-600 hover:bg-purple-700 text-white" 
                        : "text-white hover:bg-purple-800"}
                    `}
                  >
                    <Icon className="h-4 w-4" />
                    <span>{item.label}</span>
                  </Button>
                );
              })}
            </div>

            {/* Main Content Card */}
            <Card className="bg-white/10 backdrop-blur-lg border-purple-400/30 w-full">
              <CardContent className="p-0">
                {renderContent()}
              </CardContent>
            </Card>
          </div>
        </div>
      );
    };

    export default Dashboard;
    