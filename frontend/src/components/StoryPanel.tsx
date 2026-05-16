import { useState } from 'react';
import axios from 'axios';

interface Props {
  symbol: string;
}

export default function StoryPanel({ symbol }: Props) {
  const [story, setStory] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function generateStory() {
    setLoading(true);
    setError('');
    try {
      const res = await axios.post('/api/analysis/story', { symbol });
      setStory(res.data.story);
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to generate story');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="story-panel">
      <h2>Trend Story</h2>
      <button className="generate-story-btn" onClick={generateStory} disabled={loading || !symbol}>
        {loading ? 'Generating...' : 'Generate Story'}
      </button>
      {error && <div className="error-message">{error}</div>}
      {story ? (
        <div className="story-content" dangerouslySetInnerHTML={{ __html: story }} />
      ) : (
        <div className="story-placeholder">
          Click the button above to generate an AI-powered trend story for {symbol || '...'}
        </div>
      )}
    </div>
  );
}
