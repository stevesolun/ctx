---
version: alpha
name: Street Food
description: Night-market neon on butcher paper.
colors:
  primary: "#1A0F05"
  secondary: "#7A6A54"
  tertiary: "#F23C3C"
  neutral: "#F5E9CF"
  surface: "#FCF3DC"
  on-primary: "#FCF3DC"
typography:
  display:
    fontFamily: Bowlby One
    fontSize: 4.5rem
    fontWeight: 400
    letterSpacing: "-0.01em"
  h1:
    fontFamily: Bowlby One
    fontSize: 2.4rem
    fontWeight: 400
  body:
    fontFamily: DM Sans
    fontSize: 1rem
    lineHeight: 1.55
  label:
    fontFamily: DM Sans
    fontSize: 0.78rem
    fontWeight: 700
    letterSpacing: "0.06em"
rounded:
  sm: 4px
  md: 8px
  lg: 14px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

A street-food palette: butcher paper surface, neon signage accents, chunky slab display.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1A0F05`):** Headlines and core text.
- **Secondary (`#7A6A54`):** Borders, captions, and metadata.
- **Tertiary (`#F23C3C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F5E9CF`):** The page foundation.

## Typography

- **display:** Bowlby One 4.5rem
- **h1:** Bowlby One 2.4rem
- **body:** DM Sans 1rem
- **label:** DM Sans 0.78rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
