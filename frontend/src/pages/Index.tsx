    import React from 'react';
    import { Bot, Music, Shield, Calendar, MessageCircle, ArrowRight, Sparkles } from 'lucide-react';
    // Import Shadcn UI components (assuming you have them in src/components/ui)
    import { Button } from '../components/ui/button';
    import { Card, CardContent } from '../components/ui/card';
    import { useAuth, useBaseUrl } from '../App'; // Import contexts

    const Index = () => {
      const { isDiscordLinked, setCurrentPage } = useAuth();
      const { BASE_URL } = useBaseUrl();

      const features = [
        {
          icon: Music,
          title: "Spotify Integration",
          description: "Stream high-quality music directly from Spotify with seamless playlist management and queue controls."
        },
        {
          icon: MessageCircle,
          title: "AI Chat Assistant",
          description: "Intelligent command helper that answers questions and guides users through bot features using natural language."
        },
        {
          icon: Sparkles,
          title: "Voice Greetings",
          description: "Automated AI-powered voice greetings when users join voice channels, creating a welcoming atmosphere."
        },
        {
          icon: Calendar,
          title: "Smart Reminders",
          description: "Schedule daily, weekly, or custom announcements to keep your community engaged and organized."
        },
        {
          icon: Shield,
          title: "Security Monitoring",
          description: "Real-time monitoring for suspicious activities with intelligent alerts to keep your server safe."
        }
      ];

      const handleGetStarted = () => {
        if (isDiscordLinked) {
          setCurrentPage('dashboard'); // If already linked, go to dashboard
        } else {
          window.location.href = BASE_URL + 'login/discord'; // Otherwise, start Discord OAuth
        }
      };

      return (
        <div className="min-h-screen bg-gradient-to-br from-purple-900 via-blue-900 to-indigo-900 flex flex-col justify-center items-center py-12">
          {/* Hero Section */}
          <div className="container mx-auto px-6 py-12 text-center max-w-4xl">
            <h1 className="text-5xl font-extrabold text-white leading-tight mb-4 animate-fade-in">
              Harmony AI Jams
            </h1>
            <p className="text-xl text-purple-200 mb-8 animate-fade-in delay-100">
              Your AI-Enhanced Discord Music Bot & Community Assistant
            </p>
            <div className="flex flex-col sm:flex-row gap-4 justify-center animate-fade-in delay-200">
              <Button size="lg" className="bg-purple-600 hover:bg-purple-700 text-white" onClick={handleGetStarted}>
                Get Started Free <ArrowRight className="ml-2 h-5 w-5" />
              </Button>
              <Button variant="outline" size="lg" className="text-white border-purple-400 hover:bg-purple-800">
                View Documentation
              </Button>
            </div>
          </div>

          {/* Features Section */}
          <div className="container mx-auto px-6 py-16">
            <h2 className="text-4xl font-bold text-center text-white mb-12">
              Powerful Features for Your Community
            </h2>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-8">
              {features.map((feature, index) => {
                const Icon = feature.icon;
                return (
                  <Card key={index} className="bg-white/10 backdrop-blur-lg border-purple-400/30 text-white p-6 rounded-xl shadow-lg hover:shadow-xl transition-all duration-300">
                    <CardContent className="p-0 flex flex-col items-center text-center">
                      <Icon className="h-12 w-12 text-purple-400 mb-4" />
                      <h3 className="text-xl font-semibold mb-2">{feature.title}</h3>
                      <p className="text-purple-200 text-sm">{feature.description}</p>
                    </CardContent>
                  </Card>
                );
              })}
            </div>
          </div>

          {/* Stats Section */}
          <div className="container mx-auto px-6 py-16 text-center">
            <h2 className="text-4xl font-bold text-white mb-12">
              Trusted by Communities Worldwide
            </h2>
            <div className="flex flex-wrap justify-center gap-12">
              {[
                { label: "Active Servers", value: "10K+" },
                { label: "Happy Users", value: "100K+" },
                { label: "Songs Played", value: "1M+" },
                { label: "Uptime", value: "99.9%" }
              ].map((stat, index) => (
                <div key={index} className="text-center">
                  <div className="text-3xl md:text-4xl font-bold text-white">{stat.value}</div>
                  <div className="text-purple-200">{stat.label}</div>
                </div>
              ))}
            </div>
          </div>

          {/* CTA Section */}
          <div className="mt-20 text-center">
            <Card className="bg-gradient-to-r from-purple-800/50 to-blue-800/50 backdrop-blur-lg border-purple-400/30 max-w-2xl mx-auto">
              <CardContent className="p-8">
                <h2 className="text-3xl font-bold text-white mb-4">
                  Ready to Transform Your Server?
                </h2>
                <p className="text-purple-200 mb-6">
                  Join thousands of Discord communities already using our AI-enhanced music bot 
                  to create amazing experiences for their members.
                </p>
                <div className="flex flex-col sm:flex-row gap-4 justify-center">
                  <Button size="lg" className="bg-purple-600 hover:bg-purple-700 text-white" onClick={handleGetStarted}>
                    Get Started Free
                  </Button>
                  <Button variant="outline" size="lg" className="text-white border-purple-400 hover:bg-purple-800">
                    View Documentation
                  </Button>
                </div>
              </CardContent>
            </Card>
          </div>
        </div>
      );
    };

    export default Index;
    