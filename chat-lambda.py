const { KendraClient, QueryCommand, RetrieveCommand } = require("@aws-sdk/client-kendra");
const { BedrockRuntimeClient, InvokeModelCommand } = require("@aws-sdk/client-bedrock-runtime");

const kendra = new KendraClient();
const bedrock = new BedrockRuntimeClient({ region: "us-east-1" }); // Use your preferred region

// Max number of document snippets to include in context
const MAX_SNIPPETS = 3;

// Helper to ensure text doesn't exceed max length
const truncateText = (text, maxLength = 500) => {
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength) + "...";
};

// Improves formatting of text responses
const enhanceFormatting = (text) => {
  // Ensure proper spacing around headings
  text = text.replace(/([^\n])(#{1,3} )/g, '$1\n\n$2');
  text = text.replace(/(#{1,3} .*)\n([^\n#])/g, '$1\n\n$2');
  
  // Ensure proper spacing for bullet points and numbered lists
  text = text.replace(/([^\n])\n(- )/g, '$1\n\n$2');
  text = text.replace(/([^\n])\n(\d+\. )/g, '$1\n\n$2');
  
  // Add line breaks between sentences in long paragraphs (over 150 chars)
  const paragraphs = text.split('\n\n');
  const formattedParagraphs = paragraphs.map(paragraph => {
    if (paragraph.length > 150 && !paragraph.startsWith('#') && !paragraph.startsWith('-') && !paragraph.startsWith('*') && !paragraph.match(/^\d+\./)) {
      // Split long paragraphs at sentence boundaries for readability
      return paragraph.replace(/\.(\s+)([A-Z])/g, '.\n$2');
    }
    return paragraph;
  });
  text = formattedParagraphs.join('\n\n');
  
  // Ensure code blocks have proper spacing
  text = text.replace(/([^\n])\n```/g, '$1\n\n```');
  text = text.replace(/```\n([^\n])/g, '```\n\n$1');
  
  return text;
};

// Constructs a prompt for Claude with retrieved document context
const constructPrompt = (query, retrievedDocs, conversationHistory = []) => {
  // Add conversation history for context
  let historyContext = "";
  if (conversationHistory.length > 0) {
    historyContext = "Previous conversation:\n";
    conversationHistory.forEach(exchange => {
      historyContext += `Human: ${exchange.question}\nAssistant: ${exchange.answer}\n`;
    });
    historyContext += "\n";
  }

  // Add document context
  let docsContext = "Relevant documents:\n";
  retrievedDocs.forEach((doc, i) => {
    docsContext += `[Document ${i + 1}: ${doc.title}]\n${doc.excerpt}\n\n`;
  });

  // Construct the full prompt with formatting instructions
  return `${historyContext}${docsContext}

You are an IT documentation assistant for EOTSS (Executive Office of Technology Services and Security).
Answer the following question using ONLY the information from the document extracts provided above.
If the answer is not in the documents, say "I don't have that information in our documentation."

FORMAT YOUR RESPONSE:
- Use markdown formatting to make your answer readable
- Use ## headings for main sections
- Break your response into short paragraphs with line breaks between them
- Use bullet points (- ) for lists of items
- Use numbered lists (1. 2. 3.) for sequential steps or processes
- Use code blocks (```) for any commands, syntax, or technical configurations
- Bold important terms or concepts using **bold text**

Be concise and professional in your response.

QUESTION:
${query}`;
};

const lambda_handler = async (event, context) => {
  console.log("Received event:", JSON.stringify(event, null, 2));
  
  try {
    // Get the query from the request
    let query = "";
    if (event.body) {
      const body = typeof event.body === 'string' ? JSON.parse(event.body) : event.body;
      query = body.query || "";
    } else {
      query = event.query || "";
    }
    
    if (!query) {
      return formatResponse(400, { error: "Query parameter is required" });
    }
    
    console.log("Processing query:", query);
    
    // Get Knowledge Base ID from environment variable
    const KB_ID = process.env.KNOWLEDGE_BASE_ID;
    if (!KB_ID) {
      return formatResponse(500, { error: "Knowledge Base ID is not configured" });
    }
    
    // Retrieve relevant information from the knowledge base
    const retrieveResponse = await bedrock.send(new RetrieveCommand({
      knowledgeBaseId: KB_ID,
      retrievalQuery: {
        text: query
      },
      retrievalConfiguration: {
        vectorSearchConfiguration: {
          numberOfResults: MAX_SNIPPETS
        }
      }
    }));
    
    // Extract retrieved content and sources
    const retrievedTexts = [];
    const sources = [];
    
    for (const result of retrieveResponse.retrievalResults || []) {
      const content = result.content?.text || "";
      if (content) {
        retrievedTexts.push(content);
      }
      
      const sourceInfo = result.location?.s3Location;
      if (sourceInfo) {
        const uri = sourceInfo.uri || "";
        sources.push({
          uri,
          filename: uri.split('/').pop() || "Unknown",
          snippet: content.length > 100 ? content.substring(0, 100) + "..." : content
        });
      }
    }
    
    if (retrievedTexts.length === 0) {
      console.log("No relevant documents found for the query");
      
      // Even if no docs found, still ask Claude to respond appropriately
      const noDocsContext = "No relevant documents found related to this query.";
      
      const MODEL_ID = process.env.MODEL_ID || 'anthropic.claude-3-sonnet-20240229-v1:0';
      
      const response = await bedrock.send(new InvokeModelCommand({
        modelId: MODEL_ID,
        body: JSON.stringify({
          anthropic_version: "bedrock-2023-05-31",
          max_tokens: 1000,
          temperature: 0.0,
          messages: [
            {
              role: "user",
              content: `You are an IT documentation assistant. The user asked: "${query}" but I couldn't find any relevant documents about this topic. Please politely explain that you don't have information about this topic in the documentation. Format your response with proper paragraphs and spacing for readability.`
            }
          ]
        })
      }));
      
      const responseBody = JSON.parse(new TextDecoder().decode(response.body));
      let answer = responseBody.content[0].text;
      
      // Enhance formatting even for "not found" responses
      answer = enhanceFormatting(answer);
      
      return formatResponse(200, { answer, sources: [] });
    }
    
    // Combine retrieved texts with spacing between them
    const contextText = retrievedTexts.join("\n\n---\n\n");
    console.log(`Retrieved ${retrievedTexts.length} relevant document chunks`);
    
    // Create prompt for Claude
    const prompt = constructPrompt(query, retrievedTexts.map((text, i) => ({
      title: sources[i]?.filename || `Document ${i+1}`,
      excerpt: text
    })));
    
    // Invoke Claude model through Bedrock
    const MODEL_ID = process.env.MODEL_ID || 'anthropic.claude-3-sonnet-20240229-v1:0';
    
    const response = await bedrock.send(new InvokeModelCommand({
      modelId: MODEL_ID,
      body: JSON.stringify({
        anthropic_version: "bedrock-2023-05-31",
        max_tokens: 1000,
        temperature: 0.0,
        messages: [
          {
            role: "user",
            content: prompt
          }
        ]
      })
    }));
    
    // Parse Claude's response
    const responseBody = JSON.parse(new TextDecoder().decode(response.body));
    let answer = responseBody.content[0].text;
    
    // Enhance the formatting of the answer
    answer = enhanceFormatting(answer);
    
    // Return the answer with sources
    return formatResponse(200, { answer, sources });
    
  } catch (error) {
    console.error("Error:", error);
    return formatResponse(500, { error: `Internal server error: ${error.message}` });
  }
};

const formatResponse = (statusCode, body) => {
  return {
    statusCode,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "https://main.d1wmmrqd080ivk.amplifyapp.com",
      "Access-Control-Allow-Methods": "OPTIONS,POST",
      "Access-Control-Allow-Headers": "Content-Type"
    },
    body: JSON.stringify(body)
  };
};

exports.handler = lambda_handler;
